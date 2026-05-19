# ADR-0005: Operation Log Schema

## Status

Accepted

## Date

2026-05-19

## Context

Specify the operation log: schema, storage, writer/reader API, retention.

## Decision

### File layout

```
{workspace_root}/docs/operations/{JIRA_KEY}/
├── 01-design-v1.md
├── 02-design-rework-v1.md
├── 02-design-rework-v2.md
├── 03-implement-v1.md
├── 04-self-review-v1.md
├── 05-test-write-v1.md
├── 06-test-run-v1.md
├── 06-test-run-v2.md
├── 06-test-run-ESCALATED.md
├── 07-deploy-v1.md
└── 08-doc-update-v1.md
```

Filename: `{NN}-{stage-slug}-{suffix}.md`

- `NN`: zero-padded 2-digit sequence within the ticket. Increments per distinct stage entry.
- `stage-slug`: `design`, `design-rework`, `implement`, `self-review`, `test-write`, `test-run`, `pr-review-fix`, `deploy`, `doc-update`, `investigate`.
- `suffix`: `v{N}` for normal entries (revisions within the same stage instance, monotonic), `ESCALATED` for the escalation marker (no version number).

Files are Markdown with YAML frontmatter.

### Frontmatter schema

```yaml
---
jira_key: PROJ-123
stage: implement                       # canonical stage slug
revision: 2                            # 1-based; bumped on retry within same NN
status: completed                      # completed | failed | escalated
skill_invoked: mcp-implement-backend   # optional; null if no skill used
agent: claude-code                     # claude-code | copilot | cursor | direct
timestamp: 2026-05-19T15:30:00Z        # ISO-8601 UTC
duration_seconds: 240                  # wall-clock for this attempt
inputs:                                # whatever was fed into the stage
  design_doc_path: docs/designs/PROJ-123.md
  context_files:
    - src/auth/
    - src/api/login.py
  prior_log_paths:
    - docs/operations/PROJ-123/01-design-v1.md
outputs:                               # whatever the stage produced
  files_created:
    - src/auth/oauth_handler.py
  files_modified:
    - src/api/login.py
    - .env.example
  diff_summary:
    additions: 187
    deletions: 12
    files: 4
  artifacts:                           # stage-specific artifacts (URLs, IDs)
    design_issue_url: https://github.com/.../issues/45
    pr_url: https://github.com/.../pull/67
retry_context:                         # populated when revision > 1
  previous_attempts:
    - "v1: initial — failed null deref in refresh()"
  failure_signal: "test_token_refresh raised NoneType"
escalation_reason: null                # populated only when status: escalated
---
```

Pydantic model in `application/pipeline/operation_log/schema.py`:

```python
class OperationLogFrontmatter(BaseModel):
    jira_key: str
    stage: StageSlug                   # Literal of canonical slugs
    revision: int                      # >= 1
    status: Literal["completed", "failed", "escalated"]
    skill_invoked: str | None = None
    agent: AgentKind
    timestamp: datetime
    duration_seconds: float
    inputs: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    retry_context: RetryContext | None = None
    escalation_reason: str | None = None

    @model_validator(mode="after")
    def _check_escalated_consistency(self):
        if self.status == "escalated" and self.escalation_reason is None:
            raise ValueError("escalation_reason required when status=escalated")
        return self
```

### Body schema

Five required sections in fixed order:

```markdown
## What was done

Action-by-action description. Bullets. Reference files by path with line
ranges when relevant.

## Impact

What changed for the system / team / next stage. Diff stats. New env vars.
Breaking changes.

## What I could not do

Explicit gaps. Things the stage was supposed to do but didn't, with the
reason. Hidden gaps are a worse outcome than visible gaps.

## Engineering decisions

Decisions made under the design's silence, or choices between alternatives
in the repo. Decisions reviewers need to see.

## Next step

What should happen next. Concrete: which stage / which skill / what
question to answer.
```

Body validation: the operation log writer rejects any log missing one of the five sections. Each section must be non-empty (whitespace + `_(none)_` placeholder is allowed but not bare empty).

### Storage layers

Operation logs live in two places, written atomically:

#### File system (source of truth, git-friendly)

`{workspace_root}/docs/operations/{JIRA_KEY}/{filename}.md`

This is the canonical artifact. Files are committed to the ticket's branch (Stage 2+) or to a `docs/operations` shared subtree (Stage 1, which has no branch). Reviewers can navigate via standard git tooling.

#### PostgreSQL (indexed metadata, query-friendly)

Table `operation_logs`:

```sql
CREATE TABLE operation_logs (
    id              BIGSERIAL PRIMARY KEY,
    jira_key        TEXT NOT NULL,
    stage           TEXT NOT NULL,
    revision        INT NOT NULL,
    sequence_number INT NOT NULL,        -- the NN in the filename
    status          TEXT NOT NULL,
    skill_invoked   TEXT,
    agent           TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    duration_seconds REAL NOT NULL,
    inputs_json     JSONB NOT NULL DEFAULT '{}',
    outputs_json    JSONB NOT NULL DEFAULT '{}',
    retry_context   JSONB,
    escalation_reason TEXT,
    file_path       TEXT NOT NULL,        -- relative to workspace_root
    file_sha256     TEXT NOT NULL,        -- content hash for tamper detection
    body_summary    TEXT NOT NULL,        -- first 500 chars of body
    body_embedding  vector(1536),          -- for semantic search (ADR-0021)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (jira_key, sequence_number, stage, revision)
);

CREATE INDEX idx_oplogs_jira_key ON operation_logs (jira_key);
CREATE INDEX idx_oplogs_stage ON operation_logs (stage);
CREATE INDEX idx_oplogs_status ON operation_logs (status);
CREATE INDEX idx_oplogs_timestamp ON operation_logs (timestamp DESC);
CREATE INDEX idx_oplogs_body_embedding ON operation_logs USING ivfflat (body_embedding vector_cosine_ops);
```

The Markdown body itself is NOT stored in PostgreSQL (avoids data duplication and lets the file system stay authoritative). Only metadata + a 500-char summary + a vector embedding (for RAG retrieval) live in the table.

### Atomicity

A single write produces both the file and the row. The writer uses a two-phase pattern:

1. Compute `(sequence_number, revision)` by reading the existing files and DB rows; pick the next pair.
2. Write the file to a temp path; `os.rename` to the final path (atomic on POSIX, near-atomic on Windows NTFS).
3. Compute SHA-256 of the file contents.
4. `INSERT INTO operation_logs (...)` in a transaction with `(jira_key, sequence_number, stage, revision)` UNIQUE constraint.
5. If INSERT fails (duplicate), the writer logs the conflict and re-reads — this should only happen under multi-daemon race conditions, which v0.2 doesn't support (single local daemon per developer).

### OperationLogWriter API

```python
class OperationLogWriter:
    async def write(
        self,
        *,
        jira_key: str,
        stage: StageSlug,
        status: Literal["completed", "failed", "escalated"],
        agent: AgentKind,
        skill_invoked: str | None,
        duration_seconds: float,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        body: OperationLogBody,            # 5 required sections
        retry_context: RetryContext | None = None,
        escalation_reason: str | None = None,
    ) -> WrittenOperationLog:
        ...

@dataclass(frozen=True)
class OperationLogBody:
    what_was_done: str
    impact: str
    what_i_could_not_do: str
    engineering_decisions: str
    next_step: str

@dataclass(frozen=True)
class WrittenOperationLog:
    file_path: Path                       # absolute
    relative_path: str                    # relative to workspace_root
    sequence_number: int
    revision: int
    sha256: str
    db_row_id: int
```

The caller (PipelineOrchestrator) supplies the inputs / outputs / body. The writer handles sequence_number, revision, timestamp, SHA, embedding generation, file write, DB insert.

### Sequence number rules

For a given `(jira_key, stage)`:

- First entry → `sequence_number = max(existing) + 1, revision = 1`
- Retry of same stage → `sequence_number = unchanged, revision = max(existing) + 1`
- Escalation → `sequence_number = unchanged, revision = next; status=escalated; suffix=ESCALATED`

For a new stage that has never run on this ticket → new sequence_number (= max + 1).

The writer determines this by querying `operation_logs` in PostgreSQL (single round-trip).

### OperationLogReader API

```python
class OperationLogReader:
    async def list_for_ticket(self, jira_key: str) -> list[OperationLogSummary]:
        """All logs for one ticket, ordered by sequence_number then revision."""

    async def read_full(self, log_id: int) -> OperationLogFull:
        """Frontmatter + body, parsed."""

    async def count_for_stage(self, jira_key: str, stage: StageSlug) -> int:
        """Retry count source for the orchestrator."""

    async def latest_for_stage(self, jira_key: str, stage: StageSlug) -> OperationLogSummary | None:
        """The most recent log for this (ticket, stage)."""

    async def search_similar(
        self,
        query: str,
        limit: int = 10,
        filter_stage: StageSlug | None = None,
    ) -> list[OperationLogSummary]:
        """Vector search over body_summary embeddings (RAG retrieval; ADR-0021)."""
```

`OperationLogSummary` carries frontmatter + body_summary + file_path. `OperationLogFull` carries the parsed body sections.

The reader hits PostgreSQL for everything except `read_full`, which then reads the file from disk and parses sections. This keeps reads fast for the common case (orchestrator looking up retry counts, Dashboard showing timelines).

### Cross-references between logs

Rework logs reference prior logs via `retry_context.previous_attempts`. The values are 1-line summaries (not log_ids — the next attempt's context needs human-readable history, not foreign keys). PostgreSQL can still join on `(jira_key, stage)` to reconstruct the chain.

A separate `operation_log_references` table is NOT created in v0.2. If we later need explicit chain queries, we add it.

### Tamper detection

`file_sha256` is recomputed and compared whenever the reader opens a log. A mismatch is a `LogIntegrityError` — surfaced in Dashboard, logged structured, but does NOT halt the agent (an attacker who can write the file can also delete the row). The check exists for accidental corruption (rebase mishaps, editor overwrites), not security.

### Retention

Operation logs are retained indefinitely. They are small (≤ 50 KB each), and the ticket history is the value. Archive policy:

- After a Jira ticket is `DONE` for > 180 days, logs MAY be moved to `docs/operations/_archive/{YYYY}/{JIRA_KEY}/` and the DB row gets `archived_at` set.
- Archived rows are excluded from default queries but searchable via explicit `include_archived=True`.

Archive is opt-in (configurable threshold). v0.2 ships with archiving disabled.

### Migration from v0.1 / ai-coding-workflow

Logs created by the v0.1 prototype follow the same shape. The migration tool (Phase 1) reads existing `docs/operations/` files and bulk-inserts DB rows. Files are not rewritten.

## Consequences

- Operation logs are queryable (DB) and reviewable (filesystem). The Dashboard renders timelines from DB; reviewers diff Markdown.
- Sequence + revision rules give a stable filename grammar that downstream tools (the orchestrator, the Dashboard, future scripts) can rely on.
- Body sections force the agent to be explicit about gaps and decisions. Hidden gaps are caught at write time (validation rejects empty sections).
- Vector embeddings on body summaries enable "find similar past tickets" retrieval without indexing the full body text.
- Tamper detection via SHA is best-effort; correctness depends on filesystem integrity.
- Retention is unbounded by default; archive is a Day-2 operational concern, not a Day-1 ADR.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether the 500-char body summary is sufficient for the Dashboard's at-a-glance view, or whether we need a longer rendered preview | ADR-0026 |
| Q2 | Embedding model selection (matches `pgvector` 1536-dim default; may change with provider) | ADR-0021 |
| Q3 | Migration tooling for v0.1 logs (one-shot script vs Alembic data migration) | Phase 1 implementation decision |
| Q4 | What happens when the filesystem write succeeds but the DB insert fails — recovery procedure | Implementation: orphan-detection job that scans `docs/operations/` for files without DB rows |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline Business Model (OperationLogWriter is called by PipelineOrchestrator)
- ADR-0004 Stage 1 Design Flow (frontmatter usage by downstream stages)
- ADR-0019 Storage Layer (PostgreSQL schema; planned)
- ADR-0021 RAG Engine (embedding usage; planned)

## Reviewers

- [ ] Taven
