# ADR-0008: Session + Conversation Model

## Status

Accepted

## Date

2026-05-19

## Context

Define how the agent persists context across invocations: the Session lifecycle, the Conversation as a stage-scoped exchange unit, and the boundary between in-memory and persisted state.

## Decision

### Three nested scopes

```
Session                                     # cross-stage, cross-invocation, per (user, ticket)
  └── Conversation (stage-scoped)           # one per Agent Core invocation; ReAct turns live here
        └── Turn (LLM call + tool dispatch)
```

- **Session** persists from the first agent action on a ticket to the ticket reaching DONE (or being abandoned). One Session per `(user, jira_key)` pair.
- **Conversation** is the unit of one stage handler's ReAct run. Multiple Conversations within a Session (e.g., design Conversation, then implement Conversation, then test Conversation).
- **Turn** is one LLM round: messages in, assistant reply out (possibly with tool calls), tool calls dispatched, results appended. Multiple Turns per Conversation.

### Session

```python
@dataclass(frozen=True)
class Session:
    id: SessionId                            # UUID
    user_id: str                             # OS username or configured developer id
    jira_key: str
    primary_project_key: str                 # for routing
    workspace_root: Path
    mode: Literal["brownfield", "greenfield"]
    is_cross_project: bool
    created_at: datetime
    last_active_at: datetime
    status: SessionStatus                    # active | paused | closed
    metadata: dict[str, Any]                 # free-form

class SessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"                        # human marked the ticket agent-paused
    CLOSED = "closed"                        # ticket reached DONE
```

Session is created at the first Stage 1 invocation for a ticket. `SessionManager.get_or_create(user_id, jira_key)` is idempotent.

Session does NOT hold the full conversation history (that's in Conversations). Session holds:

- Identity + routing context (so stage handlers don't recompute it)
- Working Memory pointer (4-layer Memory; ADR-0020 owns the schema)
- Cumulative metrics (total tokens, total tool calls, total elapsed time)
- The Skill set loaded into this Session (one Session can accumulate skills over its lifetime)

### Conversation

```python
@dataclass
class Conversation:
    id: ConversationId
    session_id: SessionId
    stage: StageSlug                         # which stage this Conversation served
    revision: int                            # which retry/rework round
    started_at: datetime
    ended_at: datetime | None
    status: Literal["running", "completed", "failed", "escalated"]
    messages: list[Message]                  # OpenAI-format messages (system + user + assistant + tool)
    turn_count: int
    tool_call_count: int
    token_usage: TokenUsage
    operation_log_id: int | None             # set once stage completes and log written
```

`Message` follows the OpenAI chat format with role + content + optional tool_calls / tool_call_id. Messages are stored verbatim — the conversation can be replayed later (for debugging or audit).

One Conversation is created per `StageHandler.run` call. The Agent Core (ADR-0009) operates on the Conversation: appends user message, runs the ReAct loop, returns when the LLM produces a non-tool-call response.

### Turn

```python
@dataclass(frozen=True)
class Turn:
    conversation_id: ConversationId
    index: int                               # 0-based within conversation
    prompt_tokens: int
    completion_tokens: int
    tool_calls: list[ToolCallRecord]         # what was called, with arguments + result + error
    started_at: datetime
    ended_at: datetime
    latency_seconds: float
```

Turns are recorded separately so the Dashboard can render a turn-by-turn timeline. Conversations index their Turns via `index`.

### Persistence

#### PostgreSQL

```sql
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    jira_key TEXT NOT NULL,
    primary_project_key TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    mode TEXT NOT NULL,
    is_cross_project BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_active_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, jira_key)
);

CREATE TABLE conversations (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    revision INT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    messages_json JSONB NOT NULL,            -- full message array
    turn_count INT NOT NULL DEFAULT 0,
    tool_call_count INT NOT NULL DEFAULT 0,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    operation_log_id BIGINT REFERENCES operation_logs(id)
);

CREATE INDEX idx_conv_session ON conversations (session_id);
CREATE INDEX idx_conv_stage ON conversations (stage);

CREATE TABLE turns (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    index INT NOT NULL,
    prompt_tokens INT NOT NULL,
    completion_tokens INT NOT NULL,
    tool_calls_json JSONB NOT NULL DEFAULT '[]',
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    latency_seconds REAL NOT NULL,
    UNIQUE (conversation_id, index)
);
```

Messages live in `conversations.messages_json` (JSONB). This is fine for v0.2 (one Conversation per stage, typically < 100 messages). If individual messages need separate row-level operations later, a `messages` table is added then.

#### Cache

In-memory `LRUSessionCache` in the daemon holds the most recent N (default 20) active Sessions to avoid round-trips. Cache writes through to PostgreSQL synchronously; reads check cache first.

### Lifecycle events

```python
class SessionManager:
    async def get_or_create(self, user_id: str, jira_key: str) -> Session: ...
    async def start_conversation(self, session_id: SessionId, stage: StageSlug, revision: int) -> Conversation: ...
    async def append_messages(self, conversation_id: ConversationId, msgs: list[Message]) -> None: ...
    async def record_turn(self, conversation_id: ConversationId, turn: Turn) -> None: ...
    async def end_conversation(self, conversation_id: ConversationId, status: str, operation_log_id: int | None) -> None: ...
    async def pause_session(self, session_id: SessionId, reason: str) -> None: ...
    async def resume_session(self, session_id: SessionId) -> None: ...
    async def close_session(self, session_id: SessionId) -> None: ...
```

Each transition emits an event on the Observability bus (ADR-0015):

```
session.created       { session_id, user_id, jira_key }
session.paused        { session_id, reason }
session.resumed       { session_id }
session.closed        { session_id }
conversation.started  { conversation_id, session_id, stage, revision }
conversation.ended    { conversation_id, status, tokens, tool_calls }
turn.recorded         { conversation_id, index, tokens, latency }
```

The Dashboard subscribes to these events; the Memory Store subscribes to `conversation.ended` to extract Working Memory updates.

### Pausing

A Session enters `PAUSED` when:

- The ticket gets the `agent-paused` Jira label (developer wants the agent to stop reacting).
- A stage handler raises `FatalError` (the orchestrator pauses the session before halting).
- 3-strike escalation halts the stage (session is paused, not closed, to preserve context for human takeover).

Resume conditions:

- The `agent-paused` label is removed.
- The orchestrator's reaction loop on next webhook checks `session.status` and skips paused Sessions.

### Closing

A Session is `CLOSED` when:

- The Jira ticket transitions to `DONE` (final).
- A developer explicitly invokes `ai-coding session close <JIRA_KEY>`.
- The ticket is deleted in Jira (sync via polling).

Closed Sessions remain queryable. Their Conversations + Turns are retained per ADR-0005 retention rules.

### Memory boundaries

| Memory layer | Scope | Survives across |
|---|---|---|
| Short-term | within one Conversation | Conversation end → discarded |
| Working | within one Session | Stage transitions; discarded on Session close |
| Episodic | cross-session | Operation logs; ticket histories — survives forever |
| Semantic | cross-session | Extracted facts about the repo / team — survives forever |

This ADR covers Short-term + Working boundaries (where Sessions and Conversations are the container). Episodic + Semantic are ADR-0020.

The Conversation holds the full Short-term Memory (its `messages` are the entire short-term state). Working Memory is a structured map on the Session record, written by the Memory Governance subsystem (ADR-0023).

### Context construction handoff

When the PipelineOrchestrator builds a `StageContext` (ADR-0003), it asks `SessionManager` for:

```python
session = await session_manager.get_or_create(user_id, jira_key)
conversation = await session_manager.start_conversation(session.id, stage, revision)
ctx = StageContext(
    ...,
    session=session,
    conversation=conversation,
    ...
)
```

After the stage handler completes:

```python
await session_manager.end_conversation(
    conversation.id,
    status=result.outcome,
    operation_log_id=written_log.db_row_id,
)
```

The Agent Core (ADR-0009) interacts directly with the `Conversation` (appends messages, records turns) and indirectly with the `Session` (reads metadata, requests Skill loads).

### Multi-conversation queries

A Session's full history is reconstructible:

```python
session_with_history = await session_manager.full_history(session_id)
# returns Session + ordered list[Conversation], each with messages
```

Used by:

- The Dashboard's per-ticket timeline view.
- The Memory Store extraction job that produces Working Memory at conversation end.
- Stage handlers that need prior stage results (typically they read operation logs, not full conversations — but the option exists for replay-style debugging).

### Failure handling

| Failure | Behavior |
|---|---|
| `start_conversation` while a prior conversation for the same `(session, stage, revision)` is `running` | reuse the existing one (idempotent retry handling) |
| `append_messages` to an ended conversation | reject; raise `ConversationEndedError` |
| Session record missing for a known jira_key (data corruption) | `SessionManager.get_or_create` re-creates with a comment in the audit log |
| Conversation messages_json exceeds 1 MB | log a warning; do not block — downstream compactor (ADR-0011) should keep messages under this in practice |

### Concurrency

v0.2 assumes one daemon per developer machine. A Session is owned by one daemon at a time. No locking required.

If a second daemon process accidentally starts on the same machine (e.g., user mistakenly runs `ai-coding daemon start` twice), PostgreSQL row-level locks during `update sessions set last_active_at = ...` serialize access; conflict appears as a warning. v0.2 does not formally support multi-daemon — the second daemon's webhook subscription should fail with a port-in-use error before this becomes an issue.

## Consequences

- Sessions and Conversations are first-class persisted entities, enabling replay, audit, and Dashboard timelines.
- Stage handlers do not own conversation state — the SessionManager does — so handler retries don't lose prior turn history.
- The Memory subsystem subscribes to `conversation.ended` events to drive its write governance, without coupling to the orchestrator.
- The boundary between in-memory (Short-term = Conversation messages) and persisted (Working / Episodic / Semantic) is explicit, making memory consistency reasoning tractable.
- Multi-daemon concurrency is deferred; v0.2 ships single-daemon.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Conversation messages_json size growth — when (and how) Compactor (ADR-0011) shrinks it | ADR-0011 |
| Q2 | Whether Working Memory is keyed by Session or by (Session, stage) | ADR-0020 |
| Q3 | Audit log table for session lifecycle (separate vs reusing `turns` table semantics) | ADR-0015 |
| Q4 | Compatibility with future hosted-server deployment that supports many users per process | Post-v0.2 design |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline Business Model (passes session into StageContext)
- ADR-0005 Operation Log Schema (operation_log_id links from Conversation)
- ADR-0009 Agent Core (consumes Conversation)
- ADR-0011 Compactor (modifies Conversation.messages)
- ADR-0015 Observability (event bus)
- ADR-0020 Memory Store four-layer (Working / Episodic / Semantic)
- ADR-0023 Memory Governance

## Reviewers

- [ ] Taven
