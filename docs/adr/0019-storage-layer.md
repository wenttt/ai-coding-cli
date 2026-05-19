# ADR-0019: Storage Layer (PostgreSQL + pgvector)

## Status

Proposed

## Date

2026-05-19

## Context

Specify the PostgreSQL storage layer: schema, extensions, connections, migrations, local deployment.

## Decision

### PostgreSQL version + extensions

PostgreSQL 16+ with extensions:

- `pgvector` ≥ 0.7.0 — vector type + IVFFlat / HNSW indexes
- `uuid-ossp` — UUID v4 generation
- `pg_trgm` — trigram indexes for fuzzy text matching

All extensions are `CREATE EXTENSION IF NOT EXISTS` in migration `0001_initial`.

### Schema overview

```
sessions
conversations          (→ sessions)
turns                  (→ conversations)
operation_logs         (→ sessions)
processed_jira_events  (idempotency)
skill_invocations      (audit, per Conversation × Skill)
memory_entries         (4-layer Memory; see ADR-0020)
rag_chunks             (indexed text + embeddings)
config_snapshots       (audit; full Config dump on daemon start)
```

### Schema definitions

#### sessions

```sql
CREATE TABLE sessions (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id               TEXT NOT NULL,
    jira_key              TEXT NOT NULL,
    primary_project_key   TEXT NOT NULL,
    workspace_root        TEXT NOT NULL,
    mode                  TEXT NOT NULL CHECK (mode IN ('brownfield', 'greenfield')),
    is_cross_project      BOOLEAN NOT NULL DEFAULT FALSE,
    status                TEXT NOT NULL CHECK (status IN ('active', 'paused', 'closed')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at             TIMESTAMPTZ,
    metadata              JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, jira_key)
);

CREATE INDEX idx_sessions_user ON sessions (user_id, status);
CREATE INDEX idx_sessions_jira_key ON sessions (jira_key);
CREATE INDEX idx_sessions_last_active ON sessions (last_active_at DESC);
```

#### conversations

```sql
CREATE TABLE conversations (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id            UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    stage                 TEXT NOT NULL,
    revision              INT NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at              TIMESTAMPTZ,
    status                TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'escalated')),
    messages_json         JSONB NOT NULL,
    turn_count            INT NOT NULL DEFAULT 0,
    tool_call_count       INT NOT NULL DEFAULT 0,
    prompt_tokens         BIGINT NOT NULL DEFAULT 0,
    completion_tokens     BIGINT NOT NULL DEFAULT 0,
    cache_hit_tokens      BIGINT NOT NULL DEFAULT 0,
    operation_log_id      BIGINT,
    llm_provider          TEXT,
    llm_model             TEXT
);

CREATE INDEX idx_conv_session ON conversations (session_id, started_at);
CREATE INDEX idx_conv_stage ON conversations (stage, started_at);
CREATE INDEX idx_conv_status_running ON conversations (status) WHERE status = 'running';
```

#### turns

```sql
CREATE TABLE turns (
    id                    BIGSERIAL PRIMARY KEY,
    conversation_id       UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    index                 INT NOT NULL,
    prompt_tokens         INT NOT NULL,
    completion_tokens     INT NOT NULL,
    cache_hit_tokens      INT NOT NULL DEFAULT 0,
    tool_calls_json       JSONB NOT NULL DEFAULT '[]',
    finish_reason         TEXT NOT NULL,
    started_at            TIMESTAMPTZ NOT NULL,
    ended_at              TIMESTAMPTZ NOT NULL,
    latency_seconds       REAL NOT NULL,
    UNIQUE (conversation_id, index)
);

CREATE INDEX idx_turns_started_at ON turns (started_at DESC);
```

#### operation_logs

```sql
CREATE TABLE operation_logs (
    id                    BIGSERIAL PRIMARY KEY,
    jira_key              TEXT NOT NULL,
    session_id            UUID REFERENCES sessions(id) ON DELETE SET NULL,
    stage                 TEXT NOT NULL,
    revision              INT NOT NULL,
    sequence_number       INT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'escalated')),
    skill_invoked         TEXT,
    agent                 TEXT NOT NULL,
    timestamp             TIMESTAMPTZ NOT NULL,
    duration_seconds      REAL NOT NULL,
    inputs_json           JSONB NOT NULL DEFAULT '{}',
    outputs_json          JSONB NOT NULL DEFAULT '{}',
    retry_context_json    JSONB,
    escalation_reason     TEXT,
    file_path             TEXT NOT NULL,
    file_sha256           TEXT NOT NULL,
    body_summary          TEXT NOT NULL,
    body_embedding        vector(1536),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at           TIMESTAMPTZ,
    UNIQUE (jira_key, sequence_number, stage, revision)
);

CREATE INDEX idx_oplogs_jira_key ON operation_logs (jira_key, sequence_number, revision);
CREATE INDEX idx_oplogs_stage ON operation_logs (stage, timestamp DESC);
CREATE INDEX idx_oplogs_status_failed ON operation_logs (status) WHERE status IN ('failed', 'escalated');
CREATE INDEX idx_oplogs_session ON operation_logs (session_id);
CREATE INDEX idx_oplogs_embedding ON operation_logs USING ivfflat (body_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_oplogs_archived ON operation_logs (archived_at) WHERE archived_at IS NULL;
```

`body_embedding` uses 1536 dimensions matching OpenAI text-embedding-3-small. If a deployment uses a different embedding model, the dimension changes via migration; the index is rebuilt.

#### processed_jira_events

```sql
CREATE TABLE processed_jira_events (
    dedup_key             TEXT PRIMARY KEY,
    jira_key              TEXT NOT NULL,
    to_status             TEXT NOT NULL,
    received_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivery_channel      TEXT NOT NULL CHECK (delivery_channel IN ('webhook', 'polling')),
    processed_at          TIMESTAMPTZ
);

CREATE INDEX idx_jira_events_jira_key ON processed_jira_events (jira_key, received_at DESC);
CREATE INDEX idx_jira_events_cleanup ON processed_jira_events (received_at) WHERE processed_at IS NOT NULL;
```

Per ADR-0029, dedup_key = sha256(jira_key + to_status + updated_at_epoch_seconds). Rows older than 7 days are GC'd by a background job.

#### skill_invocations

```sql
CREATE TABLE skill_invocations (
    id                    BIGSERIAL PRIMARY KEY,
    conversation_id       UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    skill_name            TEXT NOT NULL,
    skill_version         TEXT NOT NULL,
    source_level          TEXT NOT NULL,
    loaded_at             TIMESTAMPTZ NOT NULL,
    loaded_via            TEXT NOT NULL CHECK (loaded_via IN ('auto_preload', 'load_skill_tool')),
    body_tokens           INT NOT NULL
);

CREATE INDEX idx_skill_invocations_conv ON skill_invocations (conversation_id);
CREATE INDEX idx_skill_invocations_name ON skill_invocations (skill_name, loaded_at DESC);
```

#### memory_entries

(Full schema in ADR-0020; sketch here for cross-reference.)

```sql
CREATE TABLE memory_entries (
    id                    BIGSERIAL PRIMARY KEY,
    layer                 TEXT NOT NULL CHECK (layer IN ('working', 'episodic', 'semantic')),
    session_id            UUID REFERENCES sessions(id) ON DELETE SET NULL,
    jira_key              TEXT,
    key                   TEXT NOT NULL,
    value_json            JSONB NOT NULL,
    confidence            REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source                TEXT NOT NULL,
    grounded_facts        TEXT[],
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by         BIGINT REFERENCES memory_entries(id),
    embedding             vector(1536)
);

CREATE INDEX idx_memory_layer_session ON memory_entries (layer, session_id);
CREATE INDEX idx_memory_jira_key ON memory_entries (jira_key);
CREATE INDEX idx_memory_embedding ON memory_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_memory_active ON memory_entries (last_used_at DESC) WHERE superseded_by IS NULL;
```

#### rag_chunks

(Full schema in ADR-0021; sketch here for cross-reference.)

```sql
CREATE TABLE rag_chunks (
    id                    BIGSERIAL PRIMARY KEY,
    source_kind           TEXT NOT NULL,
    source_id             TEXT NOT NULL,
    chunk_index           INT NOT NULL,
    content               TEXT NOT NULL,
    metadata              JSONB NOT NULL DEFAULT '{}',
    embedding             vector(1536) NOT NULL,
    indexed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_kind, source_id, chunk_index)
);

CREATE INDEX idx_rag_embedding ON rag_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 200);
CREATE INDEX idx_rag_source ON rag_chunks (source_kind, source_id);
CREATE INDEX idx_rag_metadata_gin ON rag_chunks USING GIN (metadata);
```

#### config_snapshots

```sql
CREATE TABLE config_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    daemon_started_at     TIMESTAMPTZ NOT NULL,
    config_json           JSONB NOT NULL,                      -- redacted
    config_sha256         TEXT NOT NULL,
    process_id            INT NOT NULL,
    ai_coding_version     TEXT NOT NULL
);

CREATE INDEX idx_config_snapshots_started ON config_snapshots (daemon_started_at DESC);
```

A new row per daemon start; lets the Dashboard explain "this Conversation ran under config snapshot X."

### Connection management

`asyncpg` connection pool, owned by the daemon process:

```python
class PostgresPool:
    def __init__(self, dsn: SecretStr, pool_size: int = 10): ...
    async def acquire(self) -> AsyncConnection: ...
    async def transaction(self) -> AsyncTransaction: ...
    async def close(self) -> None: ...
```

Pool sizing: default 10 connections. PostgreSQL `max_connections` per Phase 1 ops doc; with one developer per daemon and pool=10, default Postgres config is plenty.

Connection lifecycle:

- Daemon startup: ping with `SELECT 1`. Failure → exit 3 with clear error.
- Per-call: acquire from pool, release on commit/rollback. Idle connections trimmed after 5 minutes.
- Daemon shutdown: drain pool, wait up to 10s for in-flight queries.

CLI one-shot mode opens a single connection per invocation (no pool needed).

### Migrations

`Alembic` for versioned, reversible migrations.

Layout:

```
migrations/postgres/
├── alembic.ini
├── env.py
└── versions/
    ├── 0001_initial.py           # all CREATE TABLE statements + extensions
    ├── 0002_skill_invocations_audit.py
    └── ...
```

Migrations run via:

```bash
ai-coding migrate up                  # apply pending
ai-coding migrate status              # list applied + pending
ai-coding migrate down --revision N   # revert
```

Daemon refuses to start if migrations are not at HEAD (clear error message).

Migration discipline:

- Forward + reverse both implemented for every migration
- Data migrations have idempotency assertions
- Breaking schema changes (column removal) get a two-step migration: add deprecation marker + cutover migration after one version

### Local deployment

`docker-compose.yml` at repo root:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_USER: ai_coding
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?required}
      POSTGRES_DB: ai_coding_cli
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - ./.local-data/postgres:/var/lib/postgresql/data

  neo4j:
    image: neo4j:5
    restart: unless-stopped
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:?required}
    ports:
      - "127.0.0.1:7474:7474"
      - "127.0.0.1:7687:7687"
    volumes:
      - ./.local-data/neo4j/data:/data
      - ./.local-data/neo4j/logs:/logs
```

Both bind to `127.0.0.1` only (per ADR-0001 C10). Data lives in workspace-local `.local-data/` (gitignored).

Onboarding:

```bash
ai-coding init                  # creates .env scaffold (ADR-0016)
docker compose up -d
ai-coding migrate up
ai-coding daemon start
```

For deployments where Docker isn't available (some corporate Windows), instructions for a manually-installed PostgreSQL + Neo4j are in `docs/operations/`.

### Backup + restore

`docker-compose run --rm postgres pg_dump -U ai_coding ai_coding_cli > backup-$(date +%F).sql`

For Neo4j: `neo4j-admin database dump neo4j`.

Restore is the reverse. Documented in `docs/operations/backup-and-restore.md`.

Backups are not automatic in v0.2 — local-only deployment, each developer responsible for their own backups. Post-v0.2 hosted deployments add automated scheduled backups.

### Indexing strategy

- B-tree indexes on every foreign key + every column used in `WHERE` / `ORDER BY`.
- `WHERE`-clauses with selective predicates get partial indexes (`WHERE status = 'failed'`).
- `JSONB` columns get GIN indexes only when actually queried by content (in v0.2: `rag_chunks.metadata` only).
- pgvector indexes: IVFFlat for v0.2 (good balance of build time vs query speed; HNSW upgrade path is a one-line migration if recall becomes the bottleneck).

`lists` parameter for IVFFlat sized as `sqrt(expected_row_count)`:

- `operation_logs`: 100 lists (assumes ~10k rows per developer over project lifetime; tune with telemetry)
- `memory_entries`: 100 lists
- `rag_chunks`: 200 lists (typically the largest table; per-repo indexed code chunks)

### Performance tuning starting points

These are starting values, not optimized for any specific deployment:

```
shared_buffers = 256MB
effective_cache_size = 1GB
work_mem = 16MB
maintenance_work_mem = 128MB
random_page_cost = 1.1            # SSD assumed
max_parallel_workers_per_gather = 2
```

PostgreSQL container starts with PG defaults; tuning happens in Phase 8 based on workload.

### Vacuum + analysis

`autovacuum` enabled (default). Manual `ANALYZE` after migrations on tables that received bulk inserts.

`pgvector` IVFFlat indexes do NOT auto-update statistics as well as B-tree; the daemon's nightly job rebuilds vector indexes when staleness is detected (post-v0.2).

### Schema evolution policy

Schema is the project's contract with stored data. Rules:

1. Renaming a column requires a two-step migration: add new column → backfill → cutover → remove old column. Never rename in place.
2. Removing a column is a breaking change; requires explicit ADR.
3. Adding a NOT NULL column to a non-empty table requires a default OR a backfill migration.
4. Indexes can be added freely. Removing an index that wasn't covering a query is fine.
5. JSONB schemas evolve without migrations (the application code parses defensively).

### Failure handling

| Failure | Behavior |
|---|---|
| Connection refused at startup | Daemon exits 3 with stderr message including DSN (masked) |
| Migration not at HEAD | Daemon exits 4 with `ai-coding migrate up` hint |
| Transaction deadlock | Retry once with backoff; if still deadlocked → `StorageIntegrityError` (Fatal) |
| Disk full | `StoragePostgresUnavailable` (Retryable); daemon continues but writes fail; surface in Dashboard |
| pgvector index corruption (rare) | Detected on read; logged as ERROR; index marked for rebuild |
| `extra="forbid"` JSONB schema mismatch | Caught at application level (Pydantic); not a DB error |

### CLI commands

```
ai-coding migrate up | down --revision N | status
ai-coding storage status              # connection health + table sizes
ai-coding storage backup [--path]
ai-coding storage restore --from <file>
ai-coding storage stats               # row counts, index sizes, vacuum status
```

## Consequences

- Single PostgreSQL instance covers all structured data + vector indexes; one DB to back up, one to monitor.
- Alembic migrations with reverse migrations make schema evolution safe.
- `docker-compose.yml` makes onboarding a single command after the .env is set up.
- pgvector IVFFlat indexes give good recall with low operational complexity; upgrade to HNSW is a migration away.
- All ports bound to 127.0.0.1; no network exposure by default.
- Backup is manual in v0.2; acceptable for single-developer local deployment, must be revisited for hosted post-v0.2.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | When to switch IVFFlat → HNSW (recall vs index build time tradeoff) | Phase 8 measurement |
| Q2 | Connection pool sizing under heavy parallel-stage workloads | Phase 8 |
| Q3 | Multi-developer hosted deployment schema variant (multi-tenancy) | Post-v0.2 design |
| Q4 | Whether to ship a managed Postgres extension catalog or document install procedure for non-Docker users | Phase 8 ops docs |

## References

- ADR-0001 System Overview (C9, C10 — local + 127.0.0.1)
- ADR-0005 Operation Log Schema (operation_logs columns)
- ADR-0008 Session + Conversation Model (sessions / conversations / turns columns)
- ADR-0016 Configuration management (StorageConfig)
- ADR-0017 Error handling taxonomy (storage error classes)
- ADR-0020 Memory Store four-layer (memory_entries detail)
- ADR-0021 RAG Engine (rag_chunks detail)
- ADR-0022 Graph DB Neo4j integration (sync with PostgreSQL)
- ADR-0029 Jira Reaction Mechanism (processed_jira_events)

## Reviewers

- [ ] Taven
