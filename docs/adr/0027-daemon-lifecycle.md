# ADR-0027: Daemon Lifecycle + CLI Mode Toggle

## Status

Proposed

## Date

2026-05-19

## Context

Specify the daemon process lifecycle (start, ready, run, reload, shutdown), platform-specific service integration (systemd / launchd / Windows Service), signal handling, the CLI's two modes (one-shot vs daemon-delegate), and the IPC between them.

## Decision

### Daemon overview

The daemon is a single Python process started by `ai-coding daemon start`. It:

- Loads config (ADR-0016)
- Opens PostgreSQL + Neo4j connection pools (ADR-0019, ADR-0022)
- Verifies migrations are at HEAD
- Starts the FastAPI app (Dashboard + API routes per ADR-0026)
- Starts the Jira reaction loop (webhook receiver + polling worker, ADR-0029)
- Starts the Neo4j sync worker (ADR-0022)
- Starts the embedding worker (ADR-0021)
- Listens for signals (SIGTERM / SIGHUP / SIGINT)

The daemon's HTTP server binds `127.0.0.1:{port}` (default 8080). It is the same port the Dashboard uses; the same FastAPI app handles both.

### State diagram

```
            ┌──────────┐
            │  STARTING │
            └─────┬────┘
                  │
       config load + DB ping + migrations
                  │
            ┌─────▼────┐
            │   READY   │  ← health check returns 200
            └─────┬────┘
                  │
              start workers + HTTP server
                  │
            ┌─────▼────┐
            │  RUNNING  │  ← serves requests, reacts to events
            └─────┬────┘
                  │
              SIGTERM / SIGINT received
                  │
            ┌─────▼────┐
            │ STOPPING  │  ← stop accepting new requests, drain
            └─────┬────┘
                  │
              drain timeout (default 10s)
                  │
            ┌─────▼────┐
            │  STOPPED  │
            └──────────┘
```

State transitions are observable via `GET /health` (ADR-0026) — the `status` field reads `starting | ready | running | stopping`.

### Startup sequence

```python
async def main() -> int:
    try:
        config = await load_config_or_exit()                      # exit code 2 on config error
    except ConfigValidationError as exc:
        print_friendly_config_error(exc, sys.stderr)
        return 2

    setup_logging(config.observability)
    setup_event_bus(config.observability)

    pg = await connect_postgres_or_exit(config.storage)            # exit code 3 on PG unavailable
    await verify_migrations_or_exit(pg)                            # exit code 4 if behind HEAD

    if config.storage.enable_neo4j:
        neo4j = await connect_neo4j_or_exit(config.storage)        # exit code 3 on Neo4j unavailable

    write_pid_file(config.daemon)                                   # ~/.ai-coding-cli/daemon.pid
    write_config_snapshot(pg, config)                               # config_snapshots row

    await register_shipped_subscribers(...)

    embedding_worker = EmbeddingWorker(...)
    sync_worker = Neo4jSyncWorker(...) if config.storage.enable_neo4j else None
    reactor = JiraReactor(...)

    tasks = [
        asyncio.create_task(embedding_worker.run()),
        asyncio.create_task(reactor.run()),
    ]
    if sync_worker:
        tasks.append(asyncio.create_task(sync_worker.run()))

    fastapi_app = build_fastapi_app(config, pg, neo4j, ...)
    fastapi_server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host=config.daemon.http_host,
            port=config.daemon.http_port,
            log_config=None,                                        # we own logging
            lifespan="off",                                          # we manage lifespan
        )
    )
    tasks.append(asyncio.create_task(fastapi_server.serve()))

    install_signal_handlers()

    emit("daemon.started", {...})

    try:
        await wait_for_stop_signal()
    except (KeyboardInterrupt, ShutdownSignal):
        pass

    emit("daemon.stopping", {...})
    await graceful_shutdown(tasks, sync_worker, reactor, embedding_worker, pg, neo4j, fastapi_server)
    emit("daemon.stopped", {...})
    return 0
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Clean shutdown |
| 1 | Generic error (caught at top level) |
| 2 | Config validation failed |
| 3 | Database unavailable (Postgres or Neo4j) |
| 4 | Migrations not at HEAD |
| 5 | Port already in use |
| 6 | Lock file held by another daemon |

CLI surfaces the code:

```
$ ai-coding daemon start
✗ Configuration error:
  - JIRA_API_TOKEN: field required
  - LLM_PRIMARY__BASE_URL: invalid URL
See .env.example for required fields.

(exit 2)
```

### Signal handling

| Signal | Behavior |
|---|---|
| SIGTERM | Initiate graceful shutdown (state → STOPPING, drain workers, close DB pools, exit 0) |
| SIGINT (Ctrl+C) | Same as SIGTERM |
| SIGHUP | Reload `project_mapping.yaml` + Jira polling cadence (per ADR-0016 v0.2 reload scope). Other config requires restart. |
| SIGUSR1 | Trigger an event-bus drain + write a debug snapshot to `~/.ai-coding-cli/logs/snapshot-{ts}.json` for troubleshooting |
| SIGQUIT | Crash dump (emit one warning event, then `os._exit(1)` without graceful drain — for unrecoverable states) |

On Windows, where POSIX signals are limited, the daemon listens for control events via `signal.signal(SIGINT/SIGBREAK)` plus a HTTP `POST /control/{action}` endpoint for `stop`, `reload`, `snapshot`, `quit`. The CLI wraps these.

### Graceful shutdown sequence

`graceful_shutdown(tasks, ...)` (default timeout 10s, configurable via `DAEMON_SHUTDOWN_TIMEOUT_SECONDS`):

```
1. emit("daemon.stopping")
2. Set state to STOPPING; health endpoint returns 503 (so external monitors see the daemon is going away)
3. Stop accepting new HTTP requests (Uvicorn graceful shutdown)
4. Stop the reactor (process queued events but don't pull new ones for > 2 seconds)
5. Stop polling worker
6. Wait for in-flight Agent.run() calls to complete (up to remaining drain timeout)
7. Stop embedding worker (finish current batch; new enqueues blocked)
8. Stop Neo4j sync worker (finish current batch; outbox rows un-applied remain for next start)
9. Drain EventBus queue (up to 3 seconds)
10. Close Neo4j driver
11. Close Postgres pool (await all connections released)
12. Remove pid file
13. emit("daemon.stopped")
14. Flush logs
15. exit 0
```

Steps 6-7 are the longest in practice. The CLI's `ai-coding daemon stop` waits with a spinner; users see progress.

### PID file + locking

`~/.ai-coding-cli/daemon.pid` (configurable directory).

On start:

- Read existing pid file if present.
- Check if the process with that PID is running AND is an ai-coding daemon (matches argv pattern).
- If yes → exit 6 ("daemon already running, pid <N>; use ai-coding daemon stop").
- If no → stale pid; remove and proceed.

On clean shutdown: pid file removed.

On crash: pid file lingers; next start detects + removes.

Cross-process detection uses `psutil` (cross-platform).

### Platform-specific supervisors

`ai-coding daemon install-service` generates platform-specific service files. v0.2 default behavior:

- **macOS**: launchd plist installed to `~/Library/LaunchAgents/com.aicodingcli.daemon.plist`. `launchctl load -w` activates on next login.
- **Linux (systemd-user)**: unit installed to `~/.config/systemd/user/ai-coding-cli.service`. `systemctl --user enable ai-coding-cli` activates on next login.
- **Windows**: a Scheduled Task entry (one-shot at logon) wrapping `ai-coding daemon start`. Future: NSSM-based proper Windows Service if user requests.

Each plist / unit / task includes:

```
ExecStart    = /path/to/ai-coding daemon start
Restart      = on-failure
RestartDelay = 10s
WorkingDirectory = ~/.ai-coding-cli
StandardOutput  = append:~/.ai-coding-cli/logs/daemon.stdout.log
StandardError   = append:~/.ai-coding-cli/logs/daemon.stderr.log
Environment      (from a small env file ~/.ai-coding-cli/daemon.env)
```

Install is opt-in via CLI; v0.2 does NOT silently install at first run. Users who don't install a service run the daemon manually per workspace as needed.

### Uninstall

`ai-coding daemon uninstall-service` reverses the install (removes plist/unit/task, doesn't touch logs / pid file / data).

### CLI delegation modes

The CLI command (e.g., `ai-coding chat "..."`) supports two execution modes:

```
[A] one-shot:  CLI is the process; spawns its own Agent + DB pool; runs to completion; exits.
[B] delegate:  CLI sends an HTTP request to a running daemon's /api/v1/cli endpoint;
               daemon spawns an Agent; CLI streams progress; CLI exits when daemon responds.
```

Selection rule:

1. If `--mode one-shot` is set → A.
2. If `--mode delegate` is set → B (errors clearly if no daemon is running).
3. Default: check `~/.ai-coding-cli/daemon.pid`. If a daemon is running → B. Otherwise → A.

This means: when a developer leaves the daemon running, all CLI commands route through it (fast — connection pools warm, caches hot). When they don't, the CLI works standalone (slower startup but no daemon dependency).

### Why both modes

**One-shot** advantages:
- No daemon to install / run.
- Standalone CI scripts work.
- Simpler debugging when something's wrong.

**Delegate** advantages:
- 5-10× faster startup (no PG pool cold start, no Neo4j connect, no embedding worker spin-up).
- Shared Session state: the daemon already has the Conversation loaded; CLI sees it via the same in-memory cache.
- Live Dashboard updates: a delegated invocation surfaces in the Dashboard's event feed in real time.
- Webhook reactions and CLI invocations on the same daemon share the same state and observability.

### CLI delegate protocol

```
POST /api/v1/cli/invoke
{
  "command": "chat",
  "args": {"user_message": "start working on PROJ-67"},
  "session_hint": null,
  "stream": true
}

Response:
  Server-Sent Events stream of:
    event: progress
    data: {"phase": "agent.started", "ticket": "PROJ-67"}

    event: progress
    data: {"phase": "turn.ended", "turn": 0, "tokens": 7234}

    event: result
    data: {"final_message": "...", "operation_log_path": "..."}

    event: done
    data: {"exit_code": 0}
```

The CLI consumes the stream, renders progress to stderr, and exits with the daemon's reported exit code.

Authentication: same as Dashboard — none in v0.2 (127.0.0.1 only); optional `light_auth_enabled` reuses the same token.

### Crash recovery

If the daemon crashes mid-run:

- In-flight Conversations: `Conversation.status = "running"` rows remain in PostgreSQL. The daemon detects these on next start and marks them `failed` with `summary="daemon crashed during run"`. Orchestrator decides to retry on next Jira event.
- Pending Jira events: `processed_jira_events` rows without `processed_at` are picked up on next polling cycle.
- Neo4j outbox: un-applied rows are processed on next sync worker start.
- Embeddings: pending embed jobs are enqueued by re-scanning rows with `embedding IS NULL`.

Recovery is automatic; no special command needed.

### Multiple daemon prevention

v0.2 supports exactly one daemon per machine (assumption per ADR-0001). The PID file lock enforces this on a per-user-home basis.

Per-workspace daemons (multiple daemons each tied to a workspace) are post-v0.2 — would require port allocation, workspace-keyed pid files, additional routing logic. Not v0.2 scope.

### Headless / non-interactive mode

`ai-coding daemon start --headless`:

- Same as normal start except:
  - `WEB_OPEN_BROWSER_ON_START` is forced to `false`
  - Guardrail confirmations auto-refuse (per ADR-0025)
  - Log format forced to JSON (regardless of LOG_FORMAT)
  - Process detaches from terminal (Unix `setsid`)

Used by CI / cron jobs / service definitions.

### Daemon status command

```
$ ai-coding daemon status
state:           running
pid:             47821
started:         2026-04-12 08:00:00 UTC
uptime:          7h 23m
port:            8080
postgres:        ok (pool 3/10 active, lag ~0.1ms)
neo4j:           ok (lag ~0.4s)
in-flight:       2 conversations
event-queue:     14 / 10000
recent-errors:   0 in last 5min
ai-coding-version: 0.2.0
```

Implementation: HTTP GET to `/health` + `/api/v1/stats`. Returns nicely formatted output for terminals; `--json` for scripts.

### CLI commands

```
ai-coding daemon start [--headless] [--port N]
ai-coding daemon stop [--timeout 10s]
ai-coding daemon restart
ai-coding daemon status [--json]
ai-coding daemon reload                          # SIGHUP
ai-coding daemon snapshot                        # SIGUSR1 (debug dump)
ai-coding daemon install-service [--start-on-login]
ai-coding daemon uninstall-service
```

### Failure handling

| Failure | Behavior |
|---|---|
| Daemon won't start (config error) | Exit 2; clear stderr; CLI status shows "not running" |
| Daemon won't start (DB error) | Exit 3; same handling |
| Daemon already running | Exit 6 with PID hint |
| Daemon crashes during run | Process exits non-zero; if service-managed, supervisor restarts after 10s |
| `daemon stop` exceeds timeout | After 10s, send SIGKILL; pid file removed; log warning |
| `daemon reload` while config has new required field | Reload errors logged; daemon continues running with old config |
| CLI delegate-mode call but daemon went down between status check and request | Fall back to one-shot transparently; log INFO; the user sees normal output |

## Consequences

- The daemon is a single Python process owning Postgres + Neo4j connections + workers + HTTP server + WebSocket + Jira reactor.
- One-shot CLI is the fallback when no daemon is running; delegate mode is the fast path when one is. Selection is automatic.
- Platform-specific service installs are opt-in; v0.2 does NOT silently install at first run.
- Graceful shutdown drains workers in a defined order with bounded timeout; crash recovery is built into next start.
- Multi-daemon-per-machine is post-v0.2; v0.2 enforces one daemon via PID file.
- Headless mode supports CI / cron jobs.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Workspace-scoped daemons (one per VS Code workspace) for developers who work on disconnected projects simultaneously | Post-v0.2 |
| Q2 | True Windows Service install via NSSM or pywin32 (currently Scheduled Task is the v0.2 simplification) | Phase 8 / user request |
| Q3 | Daemon health auto-restart heuristics (e.g., restart if memory exceeds N GB) | Phase 8 |
| Q4 | Hot-config-reload for LLM / Storage settings without restart | Post-v0.2 (touches connection pool lifecycle) |

## References

- ADR-0001 System Overview (single-daemon-per-machine assumption)
- ADR-0015 Observability (daemon.* events + logs)
- ADR-0016 Configuration management (config / SIGHUP reload scope)
- ADR-0019 Storage Layer (Postgres connection lifecycle)
- ADR-0021 RAG Engine (embedding worker)
- ADR-0022 Neo4j Graph (sync worker)
- ADR-0026 Web Dashboard surface (FastAPI app composition)
- ADR-0029 Jira Reaction Mechanism (reactor loop)

## Reviewers

- [ ] Taven
