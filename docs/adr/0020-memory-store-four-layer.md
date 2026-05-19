# ADR-0020: Memory Store Four-layer Architecture

## Status

Accepted

## Date

2026-05-19

## Context

Specify the four-layer Memory: scopes, lifetimes, contents, schemas, read/write APIs, promotion between layers.

Governance (write filter, confidence scoring, conflict detection, stale aging) is in ADR-0023. Grounding + hallucination prevention is in ADR-0024. RAG retrieval is in ADR-0021. This ADR defines the structure those subsystems operate on.

## Decision

### Four layers

```
┌─────────────────────────────────────────────────────────────┐
│  Short-term     in Conversation.messages (process memory)   │
│                 Scope: one Conversation                     │
│                 TTL: discarded when Conversation ends       │
├─────────────────────────────────────────────────────────────┤
│  Working        in memory_entries.layer = 'working'         │
│                 Scope: one Session                          │
│                 TTL: discarded when Session closes          │
├─────────────────────────────────────────────────────────────┤
│  Episodic       in memory_entries.layer = 'episodic'        │
│                 Scope: user / cross-session                 │
│                 TTL: indefinite (with stale aging)          │
├─────────────────────────────────────────────────────────────┤
│  Semantic       in memory_entries.layer = 'semantic'        │
│                 Scope: user / cross-session / cross-repo    │
│                 TTL: indefinite (with stale aging)          │
└─────────────────────────────────────────────────────────────┘
```

### Short-term

Already specified by ADR-0008: `Conversation.messages` is the short-term memory. No additional structure here. It's the LLM's immediate working set.

The Compactor (ADR-0011) compresses Short-term as it grows. The Memory subsystem extracts facts from Short-term content into Working / Episodic before the Compactor discards messages.

### Working

The Session's structured map of active facts: decisions made this Session, files inspected, open questions, current task focus. Lives in PostgreSQL but is conceptually in-memory for the Session.

Use cases:

- Stage 2 implementing per Stage 1's design references the design's `affected_modules` list via Working Memory rather than re-parsing the Issue body each turn.
- Stage 3 self-review pulls "decisions made by Stage 2" from Working Memory to compare against the design.
- Cross-stage continuity within one ticket.

Lifetime: the Memory Manager reads Working Memory at the start of each Conversation, exposes it to the StageHandler, and writes updates at end-of-conversation. When the Session is closed, Working entries either promote to Episodic (if marked durable) or are deleted.

Typical Working entry types:
- `current_design_affected_modules: list[str]`
- `decisions_made: list[{decision: str, rationale: str, turn: int, conversation_id: UUID}]`
- `open_questions: list[{question: str, asked_in_conversation: UUID, resolved: bool}]`
- `task_focus: str` — one-line "what the agent should care about right now"

### Episodic

Cross-session, indexed by user. Records of events: which tickets the user worked on, what each ticket touched, what failed and how it was resolved.

Use cases:

- "Has this user worked on similar tickets before?" — answered via vector search of past Episodic entries.
- "What was the last attempt at this stage on this ticket?" — answered by reading Episodic for the (jira_key, stage) pair.
- "What broke last time we deployed?" — query by (project, stage=deploy, status=failed) over Episodic.

Typical Episodic entry types:
- `ticket_completed: {jira_key, summary, duration, total_tokens, key_decisions_summary}` — written on Session close (status=DONE)
- `escalation_recorded: {jira_key, stage, attempts_summary, resolution}` — written on escalation resolve
- `pattern_observed: {pattern_name, file_paths, observed_in_conversation_ids[]}` — written by the agent when it notices a recurring pattern across tickets

Episodic entries are NOT updated; they're append-only. Corrections happen by writing a new entry that supersedes the old (`superseded_by` foreign key).

### Semantic

Extracted, abstracted knowledge: project conventions ("This codebase uses error code prefix `AUTH-`"), architectural facts ("The `payments` module owns idempotency tokens"), team SOPs ("Designs require two reviewer approvals before merge").

Semantic is cross-session, cross-repo when scoped that way. Use cases:

- Static Prefix (ADR-0010) reads project-scoped Semantic entries into `[PROJECT CONVENTIONS]` section.
- Stage 1 design reads Semantic facts about the affected modules to seed the design body.
- Stage 3 review uses Semantic to detect convention violations.

Typical Semantic entry types:
- `project_convention: {area, statement, examples[], source_conversations[]}`
- `architectural_fact: {module, fact, examples[], confidence}`
- `team_sop: {process, statement, source}`

Semantic entries are mutable but mutations are governed (ADR-0023) — a new value supersedes the old; conflict detection runs.

### MemoryEntry schema

```python
class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryEntryKind(StrEnum):
    # Working
    CURRENT_DESIGN = "current_design"
    DECISIONS_MADE = "decisions_made"
    OPEN_QUESTIONS = "open_questions"
    TASK_FOCUS = "task_focus"
    # Episodic
    TICKET_COMPLETED = "ticket_completed"
    ESCALATION_RECORDED = "escalation_recorded"
    PATTERN_OBSERVED = "pattern_observed"
    # Semantic
    PROJECT_CONVENTION = "project_convention"
    ARCHITECTURAL_FACT = "architectural_fact"
    TEAM_SOP = "team_sop"


@dataclass(frozen=True)
class MemoryEntry:
    id: int
    layer: MemoryLayer
    kind: MemoryEntryKind
    session_id: UUID | None              # null for cross-session Episodic / Semantic
    jira_key: str | None                 # null for cross-ticket Semantic
    scope_project_key: str | None        # null for cross-project Semantic
    key: str                             # stable identifier within (layer, kind, scope)
    value_json: dict                     # structured payload — schema varies by kind
    confidence: float                    # 0.0 .. 1.0
    source: MemorySource                 # see below
    grounded_facts: list[str]            # references to operation_logs / conversation_ids
    created_at: datetime
    last_used_at: datetime
    superseded_by: int | None
    embedding: list[float] | None        # 1536-dim; null until embedded
```

Schema differs by `kind`. Pydantic Discriminated Unions in `application/memory/schemas.py`:

```python
class TicketCompletedPayload(BaseModel):
    jira_key: str
    summary: str
    duration_seconds: float
    total_tokens: int
    key_decisions_summary: str
    affected_modules: list[str]
    # ...

class ProjectConventionPayload(BaseModel):
    area: Literal["error_handling", "logging", "tests", "naming", "deps", "other"]
    statement: str
    examples: list[ExampleReference]
    counter_examples: list[ExampleReference] = []
    # ...
```

The application code reads/writes the discriminated union; the DB stores it as JSONB.

### MemorySource

```python
class MemorySource(BaseModel):
    kind: Literal["agent_output", "tool_grounded", "compaction_extract", "human_curated"]
    conversation_id: UUID | None
    operation_log_id: int | None
    tool_call_invocation_id: str | None
    confidence_basis: str                # 1-2 sentences explaining how confidence was set
```

Source kind determines initial confidence:

| source kind | typical confidence |
|---|---|
| `tool_grounded` (read from a tool result deterministically) | 0.95 |
| `compaction_extract` (LLM-extracted from compaction; auto) | 0.65 |
| `agent_output` (LLM said something without tool grounding) | 0.40 |
| `human_curated` (user explicitly approved) | 1.00 |

Governance can adjust confidence per ADR-0023.

### Read API

```python
class MemoryReader(Protocol):
    async def get(
        self,
        *,
        layer: MemoryLayer,
        kind: MemoryEntryKind,
        key: str | None = None,
        session_id: UUID | None = None,
        jira_key: str | None = None,
        scope_project_key: str | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryEntry]: ...

    async def get_for_session(self, session_id: UUID) -> list[MemoryEntry]:
        """All Working entries for a Session, ordered by last_used_at."""

    async def get_for_ticket(self, jira_key: str) -> list[MemoryEntry]:
        """Episodic + relevant Semantic for a ticket."""

    async def search_similar(
        self,
        *,
        query_text: str,
        layer: MemoryLayer | None = None,
        kinds: list[MemoryEntryKind] | None = None,
        scope_project_key: str | None = None,
        limit: int = 10,
        min_confidence: float = 0.5,
    ) -> list[ScoredMemoryEntry]:
        """Vector search over embeddings; filtered by layer/kind/scope."""
```

`ScoredMemoryEntry` carries similarity score + the entry.

Reads update `last_used_at` (used by stale aging, ADR-0023).

### Write API

Writes go through `MemoryWriter`, which delegates to governance (ADR-0023) for filter + conflict + confidence:

```python
class MemoryWriter(Protocol):
    async def write(
        self,
        *,
        layer: MemoryLayer,
        kind: MemoryEntryKind,
        key: str,
        value: BaseModel,                # payload, validated by Pydantic per kind
        scope_session: UUID | None = None,
        scope_jira_key: str | None = None,
        scope_project_key: str | None = None,
        source: MemorySource,
        embed: bool = True,
    ) -> MemoryWriteResult: ...

    async def supersede(
        self,
        *,
        existing_id: int,
        new_value: BaseModel,
        source: MemorySource,
    ) -> MemoryWriteResult: ...

    async def extract_and_persist(
        self,
        *,
        session_id: UUID,
        messages_to_drop: list[Message],
        compaction_mode: Literal["micro", "auto"],
    ) -> ExtractionResult:
        """Hook for the Compactor (ADR-0011). Pulls structured facts from
        messages-about-to-be-dropped, writes them to Working / Episodic /
        Semantic appropriately."""
```

```python
@dataclass(frozen=True)
class MemoryWriteResult:
    outcome: Literal["written", "rejected", "deduplicated", "superseded_existing"]
    entry_id: int | None
    rejection_reason: str | None
    superseded_id: int | None
```

### Layer interactions

#### Short-term → Working

The Memory Writer's `extract_and_persist` (called by Compactor before discarding) pulls structured facts:

- Decisions found in dropped assistant messages → `Working.decisions_made`
- Open questions raised → `Working.open_questions`
- Tool results referenced multiple times → reduced summary to `Working.task_focus`

#### Working → Episodic / Semantic (promotion on Session close)

When a Session closes (ticket DONE):

1. **Always**: write a `TicketCompletedPayload` Episodic entry summarizing the Session.
2. **Patterns observed during the Session**: if `kind == PATTERN_OBSERVED` accumulated in Working AND the pattern crossed > 1 conversation, promote to Episodic.
3. **Conventions ratified by stage 5 deploys**: if a `ProjectConventionPayload` candidate appeared in Working and the Session reached DONE successfully, promote to Semantic.
4. **Open questions** that never resolved → remain in the closed Session's Working snapshot for audit; don't promote.

Promotion is governed (ADR-0023). The orchestrator triggers it on `session.closed` event.

#### Episodic → Semantic (extraction job)

A nightly job (post-v0.2, runs on demand in v0.2 via `ai-coding memory rollup`) scans recent Episodic entries and extracts repeated patterns into Semantic:

- 3+ Episodic `ESCALATION_RECORDED` entries for the same `(stage, root_cause)` → write a Semantic `ARCHITECTURAL_FACT` warning.
- 5+ Episodic `TICKET_COMPLETED` entries citing the same `decision_pattern` → write a Semantic `PROJECT_CONVENTION`.

The job uses governance (ADR-0023): confidence on extracted Semantic entries is set based on number of corroborating Episodic entries.

### Stale aging

Implemented in ADR-0023; here just the trigger:

- An entry with `last_used_at` older than 90 days is "aged."
- Aged Semantic entries are downweighted (confidence × 0.9 per aging cycle).
- Aged Episodic entries are not changed (history).
- Stale Working entries on a still-active Session are flagged for the Memory Manager.

### Memory in the Context Layer

`StaticPrefixAssembler` (ADR-0010) reads from Memory:

```python
async def assemble_static_prefix(session: Session) -> str:
    project_conventions = await memory.search_similar(
        query_text=session.task_focus,
        layer=MemoryLayer.SEMANTIC,
        kinds=[MemoryEntryKind.PROJECT_CONVENTION],
        scope_project_key=session.primary_project_key,
        limit=5,
        min_confidence=0.6,
    )
    architectural_facts = await memory.search_similar(...)
    # ...
    return render_template("static_prefix.j2", ...)
```

The Memory Writer feeds:

- `MemoryWriteSubscriber` listens on EventBus for `conversation.ended`, `compactor.*.completed`, `session.closed` events and dispatches writes.

### Concurrency

- Working writes are session-scoped; only one daemon writes per Session in v0.2.
- Episodic / Semantic writes use PostgreSQL row-level locks (transaction-level) for supersede operations.
- Embedding generation happens out-of-band (background async task per write) to avoid blocking the writer; `embedding` column starts null and is populated by the embedding worker.

### Memory snapshot

For Dashboard display + debugging:

```python
async def snapshot(session_id: UUID) -> MemorySnapshot:
    """Returns all Memory entries the Session can currently see —
    its own Working, all Episodic for the same user, all Semantic for the
    project."""
```

Used by the Dashboard's per-Session view.

### CLI commands

```
ai-coding memory list --session <UUID>
ai-coding memory show --id <id>
ai-coding memory rollup                # run Episodic→Semantic extraction
ai-coding memory search "<query>" --layer semantic --limit 5
ai-coding memory invalidate --id <id>  # mark as stale + supersede with null
```

## Consequences

- Four layers map cleanly to four scopes (Conversation / Session / cross-session / cross-repo) with clear transitions between them.
- Schema discriminated by `kind` keeps payloads type-safe while sharing the same row layout.
- Append-only Episodic + supersede-based Semantic make audit trail straightforward.
- Promotion happens at well-defined moments (Session close, nightly rollup); the agent does not write directly to Episodic / Semantic from inside a stage handler.
- Stale aging keeps Semantic from drifting silently; old facts lose weight rather than disappearing.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | When to run `memory rollup` automatically (cron-style) vs leaving it manual in v0.2 | Phase 8 ops decision |
| Q2 | Embedding model choice — if it changes, all entries need re-embedding; migration cost | Phase 4 implementation |
| Q3 | How to handle a Semantic conflict that spans multiple users (different teams contradict each other) | Post-v0.2 multi-tenant design |
| Q4 | Whether to expose Memory write/read to the LLM as tools, or only as Static Prefix content + retrieval results | Phase 4-5 implementation; lean toward retrieval-only |

## References

- ADR-0005 Operation Log Schema (operation_log_id referenced from MemoryEntry.source)
- ADR-0008 Session + Conversation Model (Short-term Memory = Conversation.messages)
- ADR-0010 Context Layer (StaticPrefixAssembler consumes Memory)
- ADR-0011 Compactor (extract_and_persist hook)
- ADR-0019 Storage Layer (memory_entries DDL)
- ADR-0021 RAG Engine (vector search internals)
- ADR-0023 Memory Governance (write filter, confidence, conflict, stale aging)
- ADR-0024 Grounding + Hallucination prevention

## Reviewers

- [ ] Taven
