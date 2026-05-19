# ADR-0015: Observability

## Status

Accepted

## Date

2026-05-19

## Context

Specify the observability subsystem: in-process event bus, structured logging, metrics, the consolidated event catalog used by other ADRs.

## Decision

### Three observability planes

```
1. Event Bus (in-process pub/sub)   → in-process subscribers
2. Structured logs (JSONL + console) → file + stderr
3. Metrics (Prometheus exposition)   → /metrics endpoint on the local daemon
```

All three are populated from the same source: code emits an `Event`. The Event flows simultaneously to (a) bus subscribers, (b) the structured logger, (c) the metrics aggregator.

### Event model

```python
@dataclass(frozen=True)
class Event:
    name: str                          # dotted: "agent.started", "turn.ended", etc.
    timestamp: datetime                # UTC, microsecond precision
    severity: Literal["debug", "info", "warning", "error", "critical"]
    payload: dict[str, Any]            # event-specific structured data
    trace_id: str | None               # propagated across an Agent invocation
    span_id: str | None                # propagated per-turn / per-tool-call
    session_id: SessionId | None
    conversation_id: ConversationId | None


class EventBus(Protocol):
    def emit(self, event: Event) -> None: ...
    def subscribe(self, pattern: str, handler: Callable[[Event], None]) -> Subscription: ...
    async def emit_async(self, event: Event) -> None: ...
    async def drain(self, timeout: float = 5.0) -> None: ...  # for shutdown
```

Bus implementation: in-process asyncio queue + sync direct dispatch fallback. Subscribers register patterns like `"agent.*"`, `"tool_call.*"`, `"compactor.auto.completed"`. Pattern matching is glob-style on dotted names.

### Trace + Span IDs

A `trace_id` is generated when `Agent.run()` is entered; propagated to every Event emitted during that invocation. A `span_id` is generated per turn (used for tool calls inside that turn). This lets the Dashboard render turn-grouped event timelines.

Trace IDs are not propagated across Jira webhook → Agent boundary in v0.2 (different runtime, different process — the Dashboard reconstructs the link via `(jira_key, conversation_id)` co-occurrence). Cross-process tracing is post-v0.2.

### Event catalog

Consolidated reference for events emitted by each subsystem.

#### Pipeline / Orchestrator

| Event | Severity | Trigger |
|---|---|---|
| `pipeline.reaction_received` | info | Jira webhook or polling delivers a state change |
| `pipeline.handler_dispatched` | info | StageHandler.run invoked |
| `pipeline.handler_completed` | info | StageResult written |
| `pipeline.handler_failed` | warning | StageResult outcome = failed |
| `pipeline.escalated` | error | retry budget exhausted; ESCALATED log written |
| `pipeline.jira_transition` | info | Jira status transition applied |
| `pipeline.cross_project_fanout` | info | Sub-tickets created for cross-project Epic |

#### Session / Conversation

| Event | Severity | Trigger |
|---|---|---|
| `session.created` | info | first Stage 1 invocation on a ticket |
| `session.paused` | info | agent-paused label or FatalError |
| `session.resumed` | info | paused label removed |
| `session.closed` | info | ticket DONE |
| `conversation.started` | info | StageHandler.run entered |
| `conversation.ended` | info | StageHandler.run returned |
| `turn.starting` | debug | before LLM call |
| `turn.ended` | info | after LLM call + tool dispatch; payload has token counts + cache_hit_tokens |
| `turn.recorded` | debug | persisted to turns table |

#### Agent Core

| Event | Severity | Trigger |
|---|---|---|
| `agent.started` | info | Agent.run() entered |
| `agent.completed` | info | terminal assistant message |
| `agent.halted` | warning | non-terminal exit (max_turns, max_tokens, fatal_error, user_abort) |
| `agent.error` | error | unhandled exception (rare; FatalError caught and surfaced) |

#### Tool Registry

| Event | Severity | Trigger |
|---|---|---|
| `tool.dispatched` | debug | before tool.call() |
| `tool.completed` | info | after tool.call() |
| `tool.refused` | warning | Action Guardrail blocked |
| `tool.timeout` | warning | tool exceeded timeout_seconds |
| `tool.error` | warning | tool raised |
| `bridge.online` | info | MCP bridge connected |
| `bridge.offline` | warning | MCP bridge disconnected |

#### Compactor

| Event | Severity | Trigger |
|---|---|---|
| `compactor.micro.started` | debug | MicroCompact triggered |
| `compactor.micro.completed` | info | MicroCompact finished |
| `compactor.auto.started` | info | AutoCompact triggered |
| `compactor.auto.completed` | info | AutoCompact finished |
| `compactor.failed` | warning | OverPreservedError or summarize failure |

#### Skill Loader

| Event | Severity | Trigger |
|---|---|---|
| `skill.preloaded` | info | auto-preloaded at conversation start |
| `skill.loaded_mid_loop` | info | LLM called load_skill |
| `skill.load_failed` | warning | missing file or tools_required unmet |
| `skill.version_drift` | warning | workspace customization behind builtin |

#### LLM Adapter

| Event | Severity | Trigger |
|---|---|---|
| `llm.request_started` | debug | before HTTP call |
| `llm.request_completed` | debug | after HTTP call (success or error) |
| `llm.rate_limited` | warning | 429 received |
| `llm.fallback_engaged` | warning | switched to fallback adapter |

#### Memory + RAG (planned subsystems)

| Event | Severity | Trigger |
|---|---|---|
| `memory.written` | info | new entry to Working / Episodic / Semantic |
| `memory.conflict_detected` | warning | new entry conflicts with existing |
| `memory.stale_aged` | debug | entry downweighted |
| `rag.retrieved` | debug | vector or graph retrieval performed |
| `rag.embedding_failed` | warning | embedding API failed |

#### Guardrails

| Event | Severity | Trigger |
|---|---|---|
| `guardrail.input.blocked` | warning | Input Guardrail blocked user message |
| `guardrail.output.blocked` | warning | Output Guardrail blocked assistant response |
| `guardrail.action.confirmation_required` | info | destructive tool needs confirmation |
| `guardrail.action.confirmed` | info | user confirmed |
| `guardrail.action.refused` | warning | user declined |

#### Daemon lifecycle

| Event | Severity | Trigger |
|---|---|---|
| `daemon.started` | info | HTTP server bound, ready |
| `daemon.stopping` | info | SIGTERM received |
| `daemon.stopped` | info | clean shutdown |
| `daemon.webhook_received` | debug | Jira webhook hit |
| `daemon.polling_cycle` | debug | poll completed |

### Structured logging

Backed by `structlog` with JSON renderer in production, console renderer in development.

Configuration:

```python
log_format: Literal["json", "console"] = "json" in prod, "console" in dev
log_level: str = "INFO"
log_destinations: list = ["stderr", "file"]    # file at ~/.ai-coding-cli/logs/daemon.log with rotation
```

Every Event emitted is also logged with severity → log level mapping:

| Event severity | Log level |
|---|---|
| debug | DEBUG |
| info | INFO |
| warning | WARNING |
| error | ERROR |
| critical | CRITICAL |

Log records carry the full Event payload + `trace_id` + `span_id` for correlation.

`tiktoken`-counted token values, model IDs, durations, latencies are first-class log fields (not nested in a "details" object). This makes log search via `jq` or Loki straightforward.

Sensitive fields (API keys, full request bodies of LLM calls) are auto-redacted by a logging filter — Pydantic `SecretStr` types are stringified to `"***"`; known sensitive keys (`*_api_key`, `*_token`, `*_secret`) are matched and redacted.

### Metrics

Prometheus exposition on `127.0.0.1:8081/metrics` (separate port from the Dashboard's 8080 so scrape configs are independent).

Metric families:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `ai_coding_agent_invocations_total` | counter | stage, outcome | Agent.run() calls |
| `ai_coding_turn_duration_seconds` | histogram | stage, provider | per-turn wall time |
| `ai_coding_turn_tokens_prompt` | histogram | provider, model | per-turn prompt tokens |
| `ai_coding_turn_tokens_completion` | histogram | provider, model | per-turn completion tokens |
| `ai_coding_turn_cache_hit_ratio` | histogram | provider | cache_hit_tokens / prompt_tokens |
| `ai_coding_tool_calls_total` | counter | tool_name, status | tool dispatches |
| `ai_coding_tool_duration_seconds` | histogram | tool_name | per-tool latency |
| `ai_coding_compactor_runs_total` | counter | mode, outcome | Micro / Auto compaction runs |
| `ai_coding_compactor_tokens_dropped` | histogram | mode | tokens removed per compaction |
| `ai_coding_jira_reactions_total` | counter | from_status, to_status | Jira state transitions reacted to |
| `ai_coding_escalations_total` | counter | stage | 3-strike escalations |
| `ai_coding_pipeline_stage_duration_seconds` | histogram | stage, outcome | wall time per stage |
| `ai_coding_memory_writes_total` | counter | layer, outcome | Memory writes by layer |
| `ai_coding_rag_retrievals_total` | counter | source (vector / graph / hybrid) | retrieval calls |

Exposition is opt-out (`METRICS_ENABLED=false` to disable). v0.2 default: enabled.

### Subscribers shipped with the package

The daemon registers these subscribers at startup:

1. **StructuredLogSubscriber** — converts Events to log records.
2. **PrometheusSubscriber** — updates metric families based on Events.
3. **MemoryWriteSubscriber** — listens on `conversation.ended`, `compactor.*.completed` to drive Memory governance.
4. **DashboardWebSocketSubscriber** — pushes Events to connected Dashboard clients in real time.

User code (Skills, custom tools) does NOT subscribe to the bus directly in v0.2; the bus is a Foundation-level internal API.

### Trace correlation

The Dashboard accepts a query `?trace_id=abc123` and renders all Events with that trace_id ordered by timestamp. This is the canonical view for debugging a single Agent.run() invocation.

The structured log file can be filtered by `jq` similarly:

```
jq 'select(.trace_id == "abc123")' daemon.log
```

### Sampling

In v0.2, no sampling. Every Event is emitted, logged, and accumulated in metrics. Volume is modest (typical Agent.run() emits ~50-200 events).

If volume grows in post-v0.2 (e.g., daemon serves many developers in a hosted deployment), debug-severity events can be sampled at the emit site.

### EventBus internals

```python
class AsyncioEventBus(EventBus):
    def __init__(self, max_queue: int = 10_000): ...

    def emit(self, event: Event) -> None:
        """Sync entry; queues for async dispatch + immediately runs sync subscribers."""

    async def _dispatch_loop(self):
        """Pulls from queue, fans out to async subscribers, handles backpressure."""
```

If the queue is full (subscribers can't keep up), `emit` falls back to dropping debug events first, then info, then warning. Errors are always emitted. A `bus.queue_full` event is emitted on first drop.

### Configuration

```python
class ObservabilityConfig(BaseModel):
    log_format: Literal["json", "console"] = "json"
    log_level: str = "INFO"
    log_file_path: Path = Path("~/.ai-coding-cli/logs/daemon.log").expanduser()
    log_file_max_bytes: int = 100_000_000             # 100 MB
    log_file_backup_count: int = 5
    metrics_enabled: bool = True
    metrics_port: int = 8081
    event_bus_queue_size: int = 10_000
    redact_keys: list[str] = ["api_key", "token", "secret", "password"]
```

### Testing

- The `MockEventBus` records all `emit`s in a list; tests assert on which events were emitted in what order.
- For metrics, tests use `prometheus_client.CollectorRegistry` per test to isolate counters.
- Structured logs in tests use `structlog.testing.capture_logs` for assertion.

### Persistence

Events are NOT persisted to PostgreSQL. Persistence comes through three other paths:

- Operation logs (per-stage; ADR-0005)
- Conversation messages + Turn records (per-Agent-run; ADR-0008)
- Memory writes (extracted facts; ADR-0020)

The structured log file is the only complete event history; it's local-only, rotated, and considered ephemeral debug data (not authoritative for audit).

### Failure handling

| Failure | Behavior |
|---|---|
| Subscriber raises during dispatch | Exception logged; other subscribers continue; offending subscriber NOT unregistered |
| Event bus queue full | Drop policy: debug → info → warning; errors always retained; emit `bus.queue_full` |
| Metrics endpoint unreachable | Daemon continues; metrics counters accumulate in memory; restart loses them |
| Log file write fails (disk full) | Fall back to stderr only; emit `logging.file_unavailable` once |

## Consequences

- One Event type drives three observability planes (bus / logs / metrics), keeping the emission API minimal.
- Trace IDs across an Agent run make debugging straightforward in both Dashboard and structured logs.
- Sensitive data redaction is centralized in the logging filter.
- v0.2 is in-process only; cross-process tracing is post-v0.2.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | OpenTelemetry adoption for spans + traces (vs the lightweight trace_id/span_id model) | Post-v0.2 |
| Q2 | Log forwarding for teams that want centralized observability (Loki / Datadog / Splunk) — what's the minimal config we ship | Post-v0.2 |
| Q3 | Metrics retention and rollup on the local daemon (memory-only counters lose state on restart) | Phase 8 |
| Q4 | Event bus serialization for cross-process scenarios (post-v0.2 hosted deployment) | Post-v0.2 |

## References

- ADR-0003 Pipeline Business Model (orchestrator events)
- ADR-0008 Session + Conversation Model (lifecycle events)
- ADR-0009 Agent Core (agent + turn events)
- ADR-0011 Compactor (compactor events)
- ADR-0012 Skill Loader (skill events)
- ADR-0013 Tool Registry (tool events)
- ADR-0014 LLM Adapter (llm events)
- ADR-0023 Memory Governance (memory events; planned)
- ADR-0025 Guardrail Layer (guardrail events; planned)

## Reviewers

- [ ] Taven
