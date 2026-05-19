# ADR-0026: Web Dashboard Surface

## Status

Proposed

## Date

2026-05-19

## Context

Specify the local Web Dashboard: tech stack, routes, frontend rendering, WebSocket push, authentication, views, build + deployment story.

The Dashboard runs as part of the daemon process, binding to `127.0.0.1` only (ADR-0001 C10). It is read-only — instructions go through the CLI (ADR-0001 §3a).

## Decision

### Process model

Dashboard is served by the same FastAPI app the daemon already runs:

```
Daemon (single process)
├── Jira webhook receiver         POST /jira/webhook
├── HTTP API for CLI delegation   /api/v1/*
└── Web Dashboard                 GET /, /tickets, /memory, ...
                                  GET /api/v1/*   (read endpoints, shared with CLI delegation)
                                  WS  /ws/events
```

Single port (default 8080). No separate dashboard process.

### Tech stack

**Backend**: FastAPI (same runtime as daemon HTTP, ADR-0027). Routes mounted at the package layer in `web/app.py`.

**Frontend**: **HTMX + Alpine.js + Tailwind CSS**, server-rendered Jinja2 templates.

Reasoning:

- No JS build pipeline (no npm, no Vite, no Webpack). HTMX swaps HTML fragments via `hx-get` / `hx-post` attributes; Alpine handles in-page reactivity (open/close panels, filtering local state). Tailwind via CDN in v0.2 (build pipeline added if/when team customizes).
- Templates compile fast; pages render in < 100ms; no client-side hydration overhead.
- The Dashboard is read-only — no form-heavy flows that would benefit from a SPA framework.

If interactivity outgrows HTMX (e.g., real-time graph visualizations the LLM team wants in v0.3), the upgrade path is to ship a minimal React island per such page without rewriting the whole Dashboard.

**Static assets**: bundled in `web/static/`; served via FastAPI's `StaticFiles`.

### Routes

```
GET   /                                   → Dashboard home (tickets in flight)
GET   /tickets                            → all tickets list (across projects)
GET   /tickets/{jira_key}                 → ticket detail
GET   /tickets/{jira_key}/timeline        → operation log timeline (HTMX partial)
GET   /tickets/{jira_key}/conversations   → all conversations for the ticket
GET   /conversations/{conversation_id}    → conversation detail (turns + messages)
GET   /conversations/{conversation_id}/turns/{turn_index}  → turn detail (tool calls + tokens)

GET   /memory                             → memory entries list (filterable)
GET   /memory/{id}                        → memory entry detail + governance trail
GET   /memory/awaiting_review             → conflict-pending entries (ADR-0023)
POST  /memory/{id}/review                 → accept / reject / defer (HTMX form)

GET   /rag                                → RAG corpus stats (chunks per source, last reindex)
GET   /rag/search?q=...                   → debug query interface

GET   /graph                              → graph node + edge counts; recent additions
GET   /graph/impact?module=...            → ImpactQuery results

GET   /skills                             → skill registry + last-loaded timestamps
GET   /skills/{name}                      → skill body + version drift status

GET   /tokens                             → token usage trends per session / stage / adapter
GET   /errors                             → error counter dashboard by code (ADR-0017)
GET   /escalations                        → escalated tickets awaiting human action

GET   /health                             → daemon health JSON (used by uptime monitor)
GET   /config                             → current config (secrets redacted)

WS    /ws/events                          → live event stream
GET   /api/v1/*                           → JSON endpoints (also used by CLI in daemon-delegate mode)
POST  /jira/webhook                       → Jira webhook receiver (ADR-0029)
POST  /confirm/{request_id}               → Guardrail confirmation receiver (ADR-0025)
GET   /metrics                            → Prometheus exposition (port 8081 per ADR-0015)
```

Routes serving HTML are gzipped + cache-control: no-store. JSON `/api/v1/*` routes set `Cache-Control: private, max-age=5` for short-lived caching.

### Views

Each view in detail:

#### Home (`/`)

- "In flight" tickets table: jira_key | status | stage | retry_count | last_active | actions
- Sidebar: today's metrics (active tickets, completed today, escalated today, total tokens today)
- Recent events feed (last 50 from `/ws/events`)

#### Ticket detail (`/tickets/{jira_key}`)

- Header: jira_key, summary, primary_project, mode, current Jira status
- Tabs: **Timeline** | **Conversations** | **Operation logs** | **Memory** | **Graph**

**Timeline tab** is the canonical per-ticket view:

```
┌── 2026-04-12 10:30:21 ── session.created ───────────────────────┐
│   user: dev1, jira: PROJ-67, mode: brownfield                   │
└─────────────────────────────────────────────────────────────────┘

┌── 2026-04-12 10:30:23 ── pipeline.handler_dispatched ──────────┐
│   stage: design, handler: BrownfieldDesignHandler               │
└─────────────────────────────────────────────────────────────────┘

  ├─ 10:30:25 ── turn 0 ── 7,234 prompt + 156 completion tokens
  ├─ 10:30:31 ── tool_call: read_jira_ticket(PROJ-67) → 234ms
  ├─ 10:30:32 ── tool_call: find_relevant_modules(...) → 412ms
  ├─ 10:30:35 ── turn 1 ── 8,012 + 89 tokens
  ├─ 10:30:38 ── tool_call: create_design_issue(...) → 1.2s
  │              [DESTRUCTIVE — guardrail awaiting_confirmation]
  │              [USER CONFIRMED — proceed]
  ├─ 10:30:42 ── turn 2 ── 8,901 + 234 tokens [completed]
  └─ 10:30:43 ── operation log written: 01-design-v1.md

┌── 2026-04-12 10:30:44 ── pipeline.handler_completed ───────────┐
│   outcome: completed, design Issue #45 opened                   │
└─────────────────────────────────────────────────────────────────┘
```

Each event row is expandable to show full payload.

#### Conversation detail (`/conversations/{id}`)

Three-pane layout:

- Left: turn list (sequential, with token counts + duration)
- Right (default): selected turn's full message exchange (system / user / assistant / tool messages, syntax-highlighted JSON for tool calls)
- Bottom: turn's tool calls table with side-effect records

#### Memory page (`/memory`)

Filters: layer (Working / Episodic / Semantic), kind, scope_project_key, jira_key, min_confidence.

Each entry row: `id | layer.kind | key | confidence | source.kind | last_used_at`.

Click → entry detail with:
- Value (Pydantic-rendered)
- Source provenance
- Governance trail (memory_governance_log entries)
- Superseded-by chain
- Embedding similarity to other entries (top 5)

#### Memory awaiting_review (`/memory/awaiting_review`)

Conflict-pending entries from ADR-0023. Each row shows new vs existing entries side-by-side with the LLM's conflict classification + buttons: **Accept new** / **Reject new** / **Defer**.

Acceptance triggers ADR-0023's supersede flow via the same MemoryWriter API.

#### Errors (`/errors`)

Counter dashboard grouped by error `code`:

| Code | Last seen | Count (24h) | Count (7d) | Trend |
|---|---|---|---|---|
| LLM_RATE_LIMIT | 5 min ago | 12 | 47 | ↗ |
| TOOL_TIMEOUT | 2 h ago | 3 | 8 | → |
| ... |

Click a code → list of operation logs that produced it.

#### Escalations (`/escalations`)

Tickets with `escalated` label. Each row shows the last operation log (always ESCALATED) + attempts_summary + a link to the Jira ticket. Action: "Resume" CLI command pre-filled for the user.

#### Token usage (`/tokens`)

Time-series charts:

- Total tokens per day (last 30 days)
- Tokens per stage (stacked: design / implement / review / test / deploy / doc_update)
- Cache hit ratio per provider
- Cost estimate (per 1k tokens × tokens × provider rate; ADR-0014 Q2 ships a pricing table by then)

#### Health (`/health`)

JSON, no template. Used by external uptime monitors and the CLI:

```json
{
  "status": "ok",
  "daemon_started_at": "2026-04-12T08:00:00Z",
  "config_snapshot_id": 47,
  "postgres": "ok",
  "neo4j": "ok",
  "llm_primary": "ok",
  "outbox_lag_seconds": 0.4,
  "ai_coding_version": "0.2.0"
}
```

### WebSocket: `/ws/events`

The Dashboard subscribes for live updates:

```
client → server (on connect):
{"kind": "subscribe", "patterns": ["pipeline.*", "turn.ended", "guardrail.*"]}

server → client (per event):
{"kind": "event", "event": <Event payload>}

server → client (keepalive):
{"kind": "ping", "ts": "..."}
```

The DashboardWebSocketSubscriber (ADR-0015) filters events server-side by the patterns and pushes matched events.

Reconnect strategy on the client: exponential backoff, max 30s; on reconnect, fetch the last 50 missed events via `GET /api/v1/events?since=<last_ts>` to backfill.

### Authentication (v0.2)

**None.** The Dashboard binds to `127.0.0.1` only. The OS user IS the user.

Threats addressed:

- ✅ Network attacker: cannot reach 127.0.0.1 from outside.
- ⚠️ Other local OS users on the same machine: can `curl localhost:8080` and see Dashboard data. v0.2 accepts this risk; explicitly documented as a single-user-per-machine assumption (per ADR-0001 deployment model).

Optional light auth (off by default, enable via `WEB_LIGHT_AUTH_ENABLED=true`):

- A token generated at first start and stored in `~/.config/ai-coding-cli/dashboard-token`.
- Browser visits `http://127.0.0.1:8080/?token=...` once; the token sets a cookie.
- All subsequent requests require the cookie.
- Token can be reset via CLI.

For multi-user OS machines or hosted deployments (post-v0.2), real authentication is required; this ADR defers that to a hosted-mode ADR.

### CSRF protection

Even on localhost, mutation endpoints (`POST /memory/{id}/review`, `POST /confirm/{request_id}`) require a CSRF token:

- Token generated server-side per session, embedded in the form rendered by Jinja.
- Verified on POST.

This prevents a malicious local web page (e.g., one the user accidentally visits) from issuing same-origin-like POSTs via `<img src="http://127.0.0.1:8080/...">` browser tricks. The cost is one form field; the protection is uniform.

### Static assets + templates

```
src/ai_coding_cli/web/
├── app.py                    # FastAPI app factory
├── routes/                   # one file per area: tickets.py, memory.py, ...
├── templates/                # Jinja2; mirrors routes/ structure
│   ├── base.html             # layout (nav, footer)
│   ├── home.html
│   ├── tickets/
│   ├── conversations/
│   ├── memory/
│   ├── errors.html
│   └── ...
└── static/
    ├── htmx.min.js           # v1.x
    ├── alpine.min.js
    ├── tailwind-config.css   # design tokens
    ├── app.css               # custom styles on top of Tailwind
    └── icons/
```

In v0.2, Tailwind is loaded via the CDN's `play.cdn`. If a team customizes design tokens, they switch to a built `tailwind.css` via `tailwindcss-cli`; configured in `web/tailwind.config.js`. The Dashboard renders either way.

### CLI command: `ai-coding web`

Launches the Dashboard:

```
ai-coding web                  → starts daemon (if not running) + opens browser to /
ai-coding web --no-open        → start, don't open browser
ai-coding web --port 9090      → override port (still 127.0.0.1)
ai-coding web --print-token    → if light auth enabled, print the token URL
```

The command is idempotent — if a daemon is already running and Dashboard is enabled, the command opens the browser.

### Configuration

Already in ADR-0016's `WebDashboardConfig`. New fields:

```python
class WebDashboardConfig(BaseSettings):
    enabled: bool = True
    port: int = 8080
    open_browser_on_start: bool = True
    light_auth_enabled: bool = False
    tailwind_mode: Literal["cdn", "local_build"] = "cdn"
    static_cache_seconds: int = 300
```

### Observability of Dashboard itself

The Dashboard emits its own events for self-monitoring:

```
dashboard.ws_client_connected   { client_id, patterns }
dashboard.ws_client_disconnected { client_id, duration_seconds }
dashboard.route_rendered        { route, duration_ms }
```

Rate of `dashboard.route_rendered` slower than 200ms is a Phase 8 alert.

### Accessibility

Targets:

- WCAG 2.1 AA compliance for the visited pages (color contrast, keyboard navigation, ARIA labels on dynamic content).
- Screen reader support for the timeline view (events have `role="article"`, time elements use `<time datetime="...">`).
- Tested with axe-core in CI (`tests/web/test_accessibility.py`).

Not a v0.2 hard gate (advisory). Phase 8 adds it as a release criterion.

### Browser support

Targets: latest Chrome / Edge / Safari (Firefox supported but not tested in CI). No IE11.

CI runs Playwright (post-v0.2 optional, per ADR-0018) against Chromium.

### Failure handling

| Failure | Behavior |
|---|---|
| Daemon route raises | FastAPI 500 + structured error log; Dashboard renders the standard error page |
| WebSocket disconnect | Client retries; server cleans up subscription |
| Template render error | 500 + log; CI catches via integration test |
| Static asset missing (e.g., HTMX CDN unreachable in air-gapped env) | Page renders without HTMX; functional but no live updates; document the local-static workaround |
| Long-running route exceeds 30s | Cancel + 504; Dashboard suggests retry |

## Consequences

- The Dashboard runs in-process with the daemon — one configuration, one port, one log stream.
- HTMX + Alpine + Tailwind keeps frontend complexity low; no JS build pipeline required for v0.2.
- Local-only deployment plus 127.0.0.1 binding makes auth optional; explicit threat model documented.
- CSRF protection on mutation endpoints handles the edge case of malicious local web pages.
- Read-only Dashboard scope (per ADR-0001) keeps the surface predictable: Dashboard surfaces state, CLI changes state.
- Real-time updates via single WebSocket feed serve every page without per-page polling.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | When to migrate from CDN Tailwind to a built version (decision point: workspace customization or accessibility audit) | Phase 8 |
| Q2 | Graph visualization library for the Neo4j impact view — Cytoscape.js vs vis-network vs static SVG | Phase 6 implementation |
| Q3 | Whether to ship a minimal cost-pricing table for `/tokens` page or defer | ADR-0014 Q2 (deferred to Phase 8) |
| Q4 | Hosted multi-user deployment auth model — SSO, OIDC, mutual TLS | Post-v0.2 |

## References

- ADR-0001 System Overview (Dashboard read-only scope, 127.0.0.1 binding)
- ADR-0015 Observability (event bus consumed by WebSocket)
- ADR-0016 Configuration management (WebDashboardConfig)
- ADR-0017 Error handling taxonomy (`/errors` view)
- ADR-0020 Memory Store four-layer (`/memory` view)
- ADR-0021 RAG Engine (`/rag` view)
- ADR-0022 Neo4j Graph (`/graph` view)
- ADR-0023 Memory Governance (`/memory/awaiting_review`)
- ADR-0025 Guardrail Layer (`/confirm/...` endpoint)
- ADR-0027 Daemon lifecycle (process model)

## Reviewers

- [ ] Taven
