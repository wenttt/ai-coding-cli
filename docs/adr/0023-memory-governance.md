# ADR-0023: Memory Governance

## Status

Accepted

## Date

2026-05-19

## Context

Specify the governance pipeline every Memory write passes through: filter, confidence assignment, conflict detection, supersede flow, stale aging, opt-in human review.

The four Memory layers themselves are in ADR-0020. This ADR is the discipline that decides what gets in, at what confidence, and how it changes over time.

## Decision

### Pipeline overview

```
MemoryWriter.write(...)
    ↓
[1] Filter        — reject obvious noise
    ↓
[2] Score         — initial confidence
    ↓
[3] Conflict      — detect existing entries that contradict / overlap
    ↓
[4] Decision      — write fresh / supersede / dedupe / reject / escalate
    ↓
[5] Persist       — Pydantic-validated payload + governance trail
    ↓
[6] Emit events   — memory.written | memory.rejected | memory.conflict_detected
```

Reads do not pass through governance (they are filtered by layer / scope / min_confidence at query time only).

### Stage 1: Filter

Drop entries that are not worth storing:

| Reject if | Reason |
|---|---|
| `value_json` serializes < 20 chars after normalization | trivial / empty |
| `value_json` is verbatim equal to an existing active entry for the same (layer, kind, scope, key) | exact duplicate |
| The agent's confidence_basis is empty AND `source.kind == "agent_output"` | uncontextualized agent claim |
| Working entry written after the Session has `status=closed` | wrong lifecycle |
| Layer is Semantic AND `scope_project_key is None` AND `kind != TEAM_SOP` | Semantic facts must be scoped |
| The value contains content from a tool result the LLM did NOT reference downstream (compaction extraction heuristic) | low-signal extraction |

Rejection returns `MemoryWriteResult(outcome="rejected", rejection_reason=...)`.

### Stage 2: Initial confidence

Set by `MemorySource.kind` table from ADR-0020:

| source kind | initial confidence |
|---|---|
| `tool_grounded` | 0.95 |
| `compaction_extract` (LLM-extracted from compaction) | 0.65 |
| `agent_output` (LLM said without grounding) | 0.40 |
| `human_curated` | 1.00 |

Modifiers applied multiplicatively:

| Condition | Modifier |
|---|---|
| `grounded_facts` contains at least one operation_log reference | × 1.10 (capped at 1.00) |
| `grounded_facts` contains at least one tool_call_invocation_id | × 1.05 |
| Extracted from an AutoCompact summary | × 0.90 (compaction already lossy) |
| Layer = Semantic AND `kind == PROJECT_CONVENTION` AND was promoted from Working at session close | × 1.10 |
| The same key was rejected ≥ 2 times in prior writes | × 0.80 (signal that this fact is not stabilizing) |

Final confidence rounded to 2 decimal places, clamped to `[0.0, 1.0]`.

### Stage 3: Conflict detection

For Working: no conflict detection (each Session is isolated; the latest write for a key wins via `supersede`).

For Episodic: no conflict detection (append-only; corrections are explicit `supersede` calls only).

For Semantic: yes — Semantic facts contradict each other and must be reconciled.

```python
async def detect_conflicts(
    *,
    new_entry: MemoryEntry,
    existing: list[MemoryEntry],
) -> list[ConflictReport]: ...

@dataclass(frozen=True)
class ConflictReport:
    existing_id: int
    similarity_score: float          # semantic similarity of value content
    kind: Literal["contradiction", "overlap", "refinement"]
    explanation: str
```

Detection method:

1. Query existing active Semantic entries matching `(kind, scope_project_key, key)` exactly → these are the same key; trigger supersede flow (Stage 4 decision).
2. Query existing active Semantic entries matching `(kind, scope_project_key)` only → load top-5 most similar by embedding distance.
3. For each candidate, run a small LLM check: "Does the new entry CONTRADICT, OVERLAP, REFINE, or stand INDEPENDENT of the existing entry?"
4. Return non-INDEPENDENT classifications as `ConflictReport`s.

The LLM check is the configured `compaction_adapter` model (cheap; ADR-0014).

`overlap` and `refinement` are non-blocking — both entries coexist, the new one cites the existing one in `grounded_facts`. `contradiction` triggers Stage 4 decision logic.

### Stage 4: Decision matrix

Inputs: confidence, conflict reports, source kind.

| Situation | Outcome |
|---|---|
| Filter rejected at Stage 1 | `rejected` — no DB write |
| Exact-key match in Semantic, new confidence > existing | `superseded_existing` — supersede existing; new becomes active |
| Exact-key match in Semantic, new confidence ≤ existing | `deduplicated` — new entry NOT written; existing `last_used_at` bumped |
| Exact-key match in Working / Episodic | layer-specific: Working supersedes silently; Episodic appends |
| Contradiction with existing Semantic AND new is `tool_grounded` with conf ≥ 0.85 | `superseded_existing` — new wins; explanation logged |
| Contradiction AND new conf < existing conf | `rejected` with `rejection_reason="contradicts higher-confidence entry"` |
| Contradiction AND both confs within ±0.10 | `awaiting_review` — write with `status=pending_review` flag; emit `memory.conflict_detected`; Dashboard shows for human resolution |
| Overlap | `written` — store both; new entry references existing in `grounded_facts` |
| Refinement | `superseded_existing` — newer, more specific entry wins |
| No conflicts | `written` |

`awaiting_review` is the only outcome that touches human attention. v0.2 ships a Dashboard view for this; if no one reviews, the new entry stays inactive (not surfaced in retrieval) until either resolution or stale-age timeout (90 days), at which point it's auto-marked `superseded` by the existing.

### Stage 5: Persist + governance trail

A successful write also inserts a `memory_governance_log` row:

```sql
CREATE TABLE memory_governance_log (
    id              BIGSERIAL PRIMARY KEY,
    memory_entry_id BIGINT NOT NULL REFERENCES memory_entries(id) ON DELETE CASCADE,
    decision        TEXT NOT NULL,             -- "written" / "rejected" / etc.
    rejection_reason TEXT,
    confidence_base REAL NOT NULL,             -- before modifiers
    confidence_final REAL NOT NULL,
    modifiers_applied JSONB NOT NULL,
    conflict_reports JSONB NOT NULL DEFAULT '[]',
    source_json     JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mgl_entry ON memory_governance_log (memory_entry_id);
CREATE INDEX idx_mgl_decision ON memory_governance_log (decision, created_at DESC);
```

Every Memory entry has a trail. Reviews + audits use this.

### Stage 6: Events

```
memory.written           { memory_entry_id, layer, kind, confidence, source }
memory.rejected          { rejection_reason, layer, kind }
memory.deduplicated      { existing_id, reason }
memory.superseded        { superseded_id, new_id }
memory.conflict_detected { new_id, conflicts: [...], decision }
memory.awaiting_review   { new_id, conflicts: [...] }
```

Subscribers: Dashboard (live timeline), structured logging, metrics (counters per outcome).

### Supersede flow

When `outcome == "superseded_existing"`:

```python
async def supersede(*, existing_id: int, new_value: BaseModel, ...) -> MemoryWriteResult:
    async with transaction():
        # 1. Insert the new entry
        new_id = await insert_memory_entry(...)
        # 2. Mark the old entry
        await set_superseded_by(existing_id, new_id)
        # 3. Optionally: enqueue re-embedding for related entries that referenced existing_id
        await enqueue_dependent_re_embed(existing_id)
    return MemoryWriteResult(outcome="superseded_existing", entry_id=new_id, superseded_id=existing_id)
```

Read queries default to `WHERE superseded_by IS NULL`; superseded entries remain in the table for audit but are not surfaced.

### Stale aging

A nightly job (`memory_stale_age_job`) scans active Memory entries:

```python
async def age_stale_entries(now: datetime) -> StaleAgingReport:
    # 1. Working entries with session.status="closed" → either promote or delete
    #    (decided at Session close time, but a safety sweep here)
    # 2. Episodic: no aging (history)
    # 3. Semantic:
    #    - last_used_at > 90 days ago AND not human_curated → confidence *= 0.9
    #    - last_used_at > 365 days ago AND confidence < 0.4 → mark superseded by null (effectively retire)
    #    - human_curated entries: never auto-aged
```

The job runs on demand in v0.2 via `ai-coding memory rollup --age`. Post-v0.2 cron.

### Human review surface

Dashboard view: `/memory/awaiting_review` lists all `memory_entries` joined with their conflict_reports JSON. Each row offers:

- "Accept new" → bump new entry to active, supersede old
- "Reject new" → mark new as superseded by old
- "Defer" → no action; entry stays pending

CLI equivalents:

```
ai-coding memory pending                       # list awaiting_review entries
ai-coding memory review --id <id> --accept|--reject|--defer
```

### Confidence floor for retrieval

Retrieval defaults to `min_confidence=0.5` (per ADR-0020 API). Subsystems can override:

| Subsystem | Effective floor |
|---|---|
| `StaticPrefixAssembler` (project conventions) | 0.6 |
| `StaticPrefixAssembler` (architectural facts) | 0.7 |
| `inject_retrieved_context` (RAG retrieval) | 0.5 |
| `mcp-investigate` skill | 0.4 (broader recall during diagnosis) |
| `mcp-self-review` skill | 0.6 |

These thresholds are configurable in `.env`:

```
GOVERNANCE_RETRIEVAL_FLOOR_DEFAULT=0.5
GOVERNANCE_RETRIEVAL_FLOOR_CONVENTIONS=0.6
GOVERNANCE_RETRIEVAL_FLOOR_INVESTIGATE=0.4
# ...
```

### Memory + grounding hook

ADR-0024 (Grounding + Hallucination prevention) operates on Memory writes:

- Before Stage 1 filter, any `MemoryEntry.value_json` claim that is not in `grounded_facts` is checked: does the agent's output text reference a tool call that produced that fact? If not, the entry is downgraded to `agent_output` source (confidence baseline 0.40) regardless of what the writer claimed.

ADR-0024 enforces this; ADR-0023 just accepts the resulting downgraded entry through normal governance.

### Bulk imports + migrations

When upgrading from a workspace that has manual notes (e.g., a team's pre-existing convention doc), bulk import:

```
ai-coding memory import --file conventions.yaml --kind project_convention --source human_curated
```

Imports skip Stage 1 filter (the user explicitly asserts they want these in) but still go through Stage 3 (conflict detection) and Stage 5 (governance log). Outcome `awaiting_review` is possible if imports contradict existing entries.

### Failure handling

| Failure | Behavior |
|---|---|
| LLM check during conflict detection fails | Treat as `unable_to_classify` — write the entry with a flag; do NOT block on this |
| `memory_governance_log` insert fails | Treat as Fatal — the audit trail is mandatory |
| Stale aging job crashes mid-batch | Idempotent retries on next run; partial progress is fine |
| `awaiting_review` queue grows beyond 100 entries | Dashboard surfaces high WARN; consider lowering write rate or raising review cadence |
| Embedding similarity for conflict detection fails | Fall back to exact-key match only; log warning |

### Testing

Unit tests:

- Confidence calculation: each modifier combination produces the expected number.
- Filter rules: each reject reason has a positive + negative test.
- Conflict decision matrix: each row has a fixture.

Integration tests:

- Full pipeline write with fake LLM returning canned conflict classifications.
- Stale aging: time-warp `last_used_at` via freezegun, verify expected confidence decay.

### CLI commands

```
ai-coding memory pending                       # awaiting_review entries
ai-coding memory review --id <id> [--accept|--reject|--defer]
ai-coding memory governance-trail --id <id>    # full governance_log for an entry
ai-coding memory import --file ... --kind ... --source ...
ai-coding memory rollup [--age|--promote]      # run aging or Episodic→Semantic extraction
```

## Consequences

- Every Memory write is filterable, scorable, conflict-checked, and auditable through one pipeline.
- Confidence modifiers turn the "source kind" baseline into a richer signal without exposing the user to manual scoring.
- Contradiction handling between Semantic facts has three deterministic outcomes (overrule, dedupe, await review); `awaiting_review` is the only path that needs human attention, keeping cognitive load low.
- The governance trail makes "why is this fact in Memory?" questions answerable.
- Stale aging is conservative (decay, not delete) and never touches Episodic history.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | LLM cost of conflict-check calls per write — at high write rates this could dominate | Phase 4 measurement; possibly batch checks per AutoCompact event |
| Q2 | Whether to also run a periodic re-evaluation of past `awaiting_review` decisions when relevant new facts arrive | Post-v0.2 |
| Q3 | Confidence calibration — are the 0.95 / 0.65 / 0.40 / 1.00 baselines accurate after real usage? | Phase 8 telemetry-driven tuning |
| Q4 | Multi-team Semantic conflicts (different teams contradict each other) — hosted post-v0.2 concern | Post-v0.2 multi-tenant design |

## References

- ADR-0014 LLM Adapter (conflict-check uses the compaction adapter)
- ADR-0015 Observability (memory events)
- ADR-0019 Storage Layer (memory_governance_log DDL)
- ADR-0020 Memory Store four-layer (the substrate this governs)
- ADR-0024 Grounding + Hallucination prevention (upstream of Stage 2)
- ADR-0026 Web Dashboard surface (awaiting_review UI)

## Reviewers

- [ ] Taven
