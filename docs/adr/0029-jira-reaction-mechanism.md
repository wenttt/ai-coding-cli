# ADR-0029: Jira Reaction Mechanism

## Status

Proposed

## Date

2026-05-19

## Context

Specify how the agent observes Jira ticket status changes and dispatches corresponding actions.

## Decision

### Two channels: webhook (primary) + polling (fallback)

#### Webhook channel

Jira posts to the local daemon's HTTP endpoint on every relevant ticket event.

- **Endpoint**: `POST 127.0.0.1:{daemon_port}/jira/webhook`
- **Trigger events**: `jira:issue_updated` with field filter on `status` and `comment` + `jira:issue_created`
- **Payload**: standard Jira webhook payload
- **Authentication**: shared secret in HTTP header `X-AI-Coding-Webhook-Secret`, configured per developer at daemon startup

Webhook delivery requires Jira to reach the developer's machine. In typical corporate networks, the daemon is behind NAT and not directly reachable from Jira. Two strategies:

1. **Local Jira proxy** — A shared internal service (one per team / one per office) holds a long-lived connection to each developer's daemon and forwards events. The daemon authenticates to the proxy on startup.
2. **Webhook-relay tunnel** — The daemon opens an outbound persistent connection (Server-Sent Events or WebSocket) to a tiny relay service that Jira can reach. Jira webhook hits the relay; the relay forwards to the daemon over the persistent connection.

For the first deployment, prefer strategy 2 with a minimal relay service hosted on the company's internal network. Strategy 1 is for teams that want fully self-contained deployments.

#### Polling channel

The daemon polls Jira for ticket changes when webhook is unavailable or has missed events.

- **Cadence**: configurable; default 60 seconds for active tickets, 300 seconds for idle assigned tickets
- **Query**: JQL `assignee = currentUser() AND updated >= -10m` (or last-seen timestamp)
- **Compares**: incoming `status`, `updated`, and `comment` count against the daemon's local cache
- **Used for**: catching up after daemon restart, after webhook gaps, in development without relay infrastructure

Polling and webhook coexist. Polling is the safety net; webhook is the primary low-latency path. When both deliver the same transition, the daemon de-duplicates by `(ticket_key, status, updated_at)` tuple.

### Reaction dispatch

The daemon receives an event and routes it to a stage handler:

```
event {ticket_key, from_status, to_status, comment?}
  ↓
dispatcher
  ↓ matches to_status
stage handler (one per status)
  ↓
agent runs ReAct loop
  ↓
on completion: transition Jira to next status + write operation log
```

Stage handlers are pure functions of (ticket payload, agent core, MCP tools, prior operation logs). They are idempotent: running the same handler against the same ticket state produces the same Jira transition (no duplicate side effects).

### Idempotency

Every reaction is guarded by a deduplication key:

```
key = sha256(ticket_key + to_status + updated_at_epoch_seconds)
```

The daemon stores observed keys in a PostgreSQL table (`processed_jira_events`) with a TTL of 7 days. Receiving the same key twice (webhook + polling, or webhook retry) is a no-op.

For agent actions that have side effects beyond Jira (e.g. opening a GitHub Issue), the handler first queries whether the side effect already exists (`find_design_issue_for_jira(jira_key)` returns an existing Issue if found). The handler only creates new artifacts when the lookup returns nothing.

### Error handling

| Error class | Behavior |
|---|---|
| Transient (network, Jira 5xx, rate limit) | Retry with exponential backoff up to 5 attempts |
| Permission denied (transition not allowed for service account) | Log + post Jira comment requesting manual transition + halt for this ticket |
| Invalid transition (status changed by another actor between read and write) | Re-read ticket state, re-evaluate, retry handler once; if still mismatched, halt + Jira comment |
| Stage handler exception | Mark ticket with `agent-error` label; write operation log with stack trace; halt for this ticket |
| Retry budget exhausted (3 stage attempts) | Add `escalated` label; halt; do not auto-transition further |

Halt for a ticket means the daemon no longer reacts to that ticket's events until the `agent-error` or `escalated` label is removed (manual signal from developer to resume).

### Daemon lifecycle interactions

The daemon's reaction loop runs as part of the daemon process started by `ai-coding daemon start`. When the daemon is not running, no reactions happen. Polling-after-restart catches up by querying recent `updated` timestamps.

The CLI's one-shot mode does not subscribe to webhooks. Developers using one-shot mode must explicitly invoke the next stage themselves; reactions only fire when the daemon is up.

### Configuration

In `.env`:

```
JIRA_WEBHOOK_SECRET=...
JIRA_RELAY_URL=https://relay.internal.company.com   # for webhook-relay tunnel
JIRA_POLLING_INTERVAL_ACTIVE_SECONDS=60
JIRA_POLLING_INTERVAL_IDLE_SECONDS=300
JIRA_REACTION_RETRY_MAX=5
JIRA_REACTION_RETRY_BASE_SECONDS=2
```

## Consequences

- Status changes anywhere (Jira UI, mobile, automation rule, the agent itself) trigger the same reaction logic. There is no second source of truth.
- Webhook + polling redundancy makes the system tolerant to relay outages and daemon restarts.
- Idempotency at the event-key level + side-effect-existence check at the handler level makes duplicate delivery harmless.
- Adopting teams need either the webhook-relay service running (one team / one office instance) or accept polling-only operation with up to 60-second latency.
- The daemon must be running for reactions to fire. Developers who only use one-shot CLI mode lose automatic reactions; they must explicitly invoke each stage.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Webhook-relay service deployment topology (one per team vs one per company) | Operational deployment doc |
| Q2 | What happens to a ticket whose status is changed while the daemon is down + polling misses it (> 10 min window) | Implementation: extend polling lookback on first poll after restart |
| Q3 | Cross-project sub-task webhook routing — does each sub-task's ticket reach the right developer's daemon | Likely handled by Jira filter on `assignee = currentUser()` |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline business model
- ADR-0028 Jira Workflow Specification (the state model this mechanism observes)
- Atlassian Jira webhook documentation

## Reviewers

- [ ] Taven
