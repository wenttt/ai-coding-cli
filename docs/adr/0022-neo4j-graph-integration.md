# ADR-0022: Neo4j Graph Integration + Sync with PostgreSQL

## Status

Proposed

## Date

2026-05-19

## Context

Specify the Neo4j graph layer: node + relationship schema, PostgreSQL-to-Neo4j sync, GraphQuery API, traversal patterns, client + migration tooling.

PostgreSQL is the source of truth (per ADR-0001 S3). Neo4j is a graph view derived from it.

## Decision

### Node labels

```
(:Ticket {jira_key, project_key, summary, status, created_at, ...})
(:Repo {owner, name, slug, primary_language})
(:Module {repo_slug, path, language, lines})
(:Symbol {repo_slug, path, name, kind, line_start, line_end})       # function / class / method
(:Endpoint {repo_slug, method, path_pattern, handler_symbol})
(:DesignIssue {issue_number, repo_slug, jira_key, state, created_at})
(:PullRequest {pr_number, repo_slug, jira_key, state, created_at, merged_at})
(:OperationLog {id, jira_key, stage, revision, status, created_at})
(:Stage {name})                                                       # enum-like nodes
(:Skill {name, version, scope})
(:User {user_id})                                                     # developer
(:Contract {contract_id, design_issue_number, type, version})
```

Node identity:

- `Ticket`: keyed by `jira_key`
- `Repo`: keyed by `(owner, name)`
- `Module`: keyed by `(repo_slug, path)`
- `Symbol`: keyed by `(repo_slug, path, name, line_start)`
- `Endpoint`: keyed by `(repo_slug, method, path_pattern)`
- `DesignIssue`: keyed by `(repo_slug, issue_number)`
- `PullRequest`: keyed by `(repo_slug, pr_number)`
- `OperationLog`: keyed by `id` (PostgreSQL operation_logs.id)
- `Skill`: keyed by `name` (latest version retains)
- `User`: keyed by `user_id`
- `Contract`: keyed by `contract_id`

Each label has a UNIQUE constraint in Neo4j on its key fields (via migration).

### Relationship types

```
(:Ticket)-[:PARENT_OF]->(:Ticket)                                    # Epic → sub-task
(:Ticket)-[:LINKED_TO]->(:Ticket)                                    # Jira issue links
(:Ticket)-[:AFFECTS_REPO {role}]->(:Repo)                            # cross-project affected
(:Ticket)-[:WORKED_BY]->(:User)
(:DesignIssue)-[:DESIGNS]->(:Ticket)
(:PullRequest)-[:CLOSES]->(:DesignIssue)
(:PullRequest)-[:IMPLEMENTS]->(:Ticket)
(:OperationLog)-[:FOR_STAGE]->(:Stage)
(:OperationLog)-[:OF_TICKET]->(:Ticket)
(:OperationLog)-[:USED_SKILL]->(:Skill)
(:Module)-[:IN_REPO]->(:Repo)
(:Module)-[:IMPORTS]->(:Module)                                      # static import graph
(:Symbol)-[:DEFINED_IN]->(:Module)
(:Symbol)-[:CALLS]->(:Symbol)                                        # static call graph
(:Endpoint)-[:HANDLED_BY]->(:Symbol)
(:Endpoint)-[:DEPENDS_ON]->(:Endpoint)                               # frontend → backend via Contract
(:Contract)-[:DEFINED_IN]->(:DesignIssue)
(:Contract)-[:EXPOSES]->(:Endpoint)
(:Contract)-[:CONSUMED_BY]->(:Repo)
```

Edges carry properties when they're useful: `:AFFECTS_REPO {role: "backend"}`, `:CALLS {confidence: 0.95, source: "static_analysis"}`.

### What PostgreSQL holds vs what Neo4j holds

```
Ticket properties (summary, description, status, ...)  → PostgreSQL only
Ticket node + relationships                            → Neo4j (projected)

OperationLog body, file_path, body_embedding           → PostgreSQL only
OperationLog node                                       → Neo4j (id + minimal projection)

Module file contents                                   → file system (workspace)
Module / Symbol nodes + edges                          → Neo4j

Memory / RAG content + embeddings                      → PostgreSQL only
                                                          (no projection to Neo4j; relations stay in PG)
```

Neo4j stores the **relationship structure** and projects the minimal subset of properties needed for graph queries. Detail-fetch follows the path back to PostgreSQL via the node's key.

### Why both, not just PostgreSQL

PostgreSQL with `ltree` / `parent_id` can handle 1-level hierarchies, but the queries we need (multi-hop module dependency, cross-project ticket linkage chains, contract → endpoint → handler symbol traversal) are 3+ hops. Neo4j's Cypher is the right tool for those.

### Sync: outbox + CDC pattern

The sync direction is one-way: PostgreSQL → Neo4j. A change in PostgreSQL produces a row in an outbox table; a worker consumes the outbox and applies the change to Neo4j.

#### Outbox schema

```sql
CREATE TABLE neo4j_outbox (
    id              BIGSERIAL PRIMARY KEY,
    op              TEXT NOT NULL CHECK (op IN ('upsert_node', 'delete_node', 'upsert_edge', 'delete_edge')),
    label_or_type   TEXT NOT NULL,
    key_json        JSONB NOT NULL,
    properties_json JSONB NOT NULL DEFAULT '{}',
    from_node_json  JSONB,                          -- for edges
    to_node_json    JSONB,                          -- for edges
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at      TIMESTAMPTZ
);

CREATE INDEX idx_outbox_pending ON neo4j_outbox (id) WHERE applied_at IS NULL;
```

Producers in the application code (PipelineOrchestrator on Jira reactions, OperationLogWriter on writes, repo indexer on `rag index`, etc.) write to the outbox in the **same PostgreSQL transaction** as the source change. Atomicity is guaranteed: if the source change rolls back, the outbox row never appears.

#### Sync worker

```python
class Neo4jSyncWorker:
    async def run(self) -> None:
        while not self.stopping:
            batch = await self.fetch_pending(limit=100)
            if not batch:
                await asyncio.sleep(0.5)
                continue
            await self.apply_to_neo4j(batch)
            await self.mark_applied([row.id for row in batch])
```

The worker:

- Polls `neo4j_outbox` every 0.5s (could be promoted to `LISTEN/NOTIFY` post-v0.2).
- Applies in batches of 100 per Neo4j transaction.
- Marks `applied_at` only after the Neo4j transaction commits.
- On failure: leaves rows un-applied; retries on next loop.

Idempotency: each operation uses `MERGE` on the node/edge key, so re-applying is a no-op.

#### Cleanup

A nightly job (`ai-coding storage gc`) deletes outbox rows with `applied_at` older than 7 days.

### Producers in detail

| Source change | Outbox op | Notes |
|---|---|---|
| Ticket created in Jira → cached in PG | `upsert_node :Ticket` | minimal props (key, status, project_key, summary, created_at) |
| Ticket status changed | `upsert_node :Ticket` | only status + closed_at properties |
| Ticket link (Jira issuelinks) | `upsert_edge :LINKED_TO` | direction per Jira's outward/inward |
| Sub-task created | `upsert_edge :PARENT_OF` | from Epic to sub-task |
| Cross-project sub-task fan-out (ADR-0006) | `upsert_edge :AFFECTS_REPO {role}` | per affected project |
| Operation log written | `upsert_node :OperationLog` + `upsert_edge :OF_TICKET` + `upsert_edge :FOR_STAGE` + (optional) `upsert_edge :USED_SKILL` | one batch |
| Design Issue opened | `upsert_node :DesignIssue` + `upsert_edge :DESIGNS` | |
| Code PR opened | `upsert_node :PullRequest` + `upsert_edge :CLOSES` (to design Issue) + `upsert_edge :IMPLEMENTS` (to ticket) | |
| Repo indexed (`rag index`) | many: `upsert_node :Repo`, `:Module`, `:Symbol` + `upsert_edge :IMPORTS`, `:DEFINED_IN`, `:CALLS`, `:HANDLED_BY` | bulk; can be 10k+ rows per repo |
| Skill loaded | `upsert_node :Skill` + `upsert_edge :USED_SKILL` from the conversation's operation log | |
| Contract section parsed in cross-project design (ADR-0004) | `upsert_node :Contract` + `:EXPOSES` (Endpoints) + `:DEFINED_IN` (DesignIssue) + `:CONSUMED_BY` (per affected Repo) | |

### Code graph indexing

`ai-coding rag index --source code_file` (ADR-0021) doubles as the code-graph indexer. After chunking + embedding:

1. For each parsed Symbol (tree-sitter result), upsert `(:Symbol)-[:DEFINED_IN]->(:Module)`.
2. For each `import` statement found in the file, upsert `(:Module)-[:IMPORTS]->(:Module)` (resolving the import target where possible; fallback to a placeholder `:Module {path: "<unresolved>"}` to be backfilled later).
3. For each function call, upsert `(:Symbol)-[:CALLS]->(:Symbol)` with `confidence` based on resolver certainty (0.95 if exact, 0.5 if heuristic).
4. For each HTTP endpoint declaration (FastAPI `@router.post(...)`, Express `app.get(...)`, etc.), upsert `(:Endpoint)-[:HANDLED_BY]->(:Symbol)`.

Indexing failures (parse errors, unresolved imports) are logged but not fatal — partial graphs are useful.

### GraphQuery API

```python
class GraphQuery(Protocol):
    """A high-level query the RAG Engine + tools dispatch."""

    def to_cypher(self) -> tuple[str, dict]: ...     # Cypher + parameters


class GraphEngine(Protocol):
    async def execute(
        self,
        query: GraphQuery | str,
        params: dict | None = None,
        timeout_seconds: float = 5.0,
    ) -> GraphResult: ...

    async def shortest_path(
        self,
        *,
        from_label: str,
        from_key: dict,
        to_label: str,
        to_key: dict,
        max_hops: int = 5,
        relationship_filter: list[str] | None = None,
    ) -> list[GraphPath]: ...

    async def neighbors(
        self,
        *,
        node_label: str,
        node_key: dict,
        relationships: list[str],
        direction: Literal["incoming", "outgoing", "both"] = "both",
        depth: int = 1,
    ) -> list[GraphNode]: ...
```

Shipped concrete `GraphQuery` subclasses for common patterns:

```python
class ImpactQuery(GraphQuery):
    """What modules + tickets are affected if I change this module?"""
    module_repo_slug: str
    module_path: str
    max_hops: int = 3

class CrossProjectChainQuery(GraphQuery):
    """What's the design Issue → PRs chain for a cross-project Epic?"""
    epic_jira_key: str

class SimilarTicketsByLinkageQuery(GraphQuery):
    """Tickets linked (multi-hop) to this one via LINKED_TO or PARENT_OF."""
    jira_key: str
    max_hops: int = 4
```

### Cypher examples

#### Impact: "If I change `src/auth/login.py`, what's affected?"

```cypher
MATCH (m:Module {repo_slug: $repo, path: 'src/auth/login.py'})
MATCH path = (m)<-[:IMPORTS|CALLS*1..3]-(dependent)
RETURN dependent, length(path) AS distance, [r IN relationships(path) | type(r)] AS via
ORDER BY distance, dependent.path
LIMIT 50
```

#### Cross-project chain

```cypher
MATCH (epic:Ticket {jira_key: $epic_jira_key})-[:PARENT_OF]->(sub:Ticket)
OPTIONAL MATCH (sub)<-[:DESIGNS]-(d:DesignIssue)
OPTIONAL MATCH (d)<-[:CLOSES]-(pr:PullRequest)
RETURN sub, d, pr
ORDER BY sub.jira_key
```

#### Contract consumers

```cypher
MATCH (c:Contract {contract_id: $contract_id})-[:CONSUMED_BY]->(r:Repo)
OPTIONAL MATCH (c)-[:EXPOSES]->(e:Endpoint)-[:DEPENDS_ON]-(other:Endpoint)
RETURN r, e, other
```

### GraphResult shape

```python
@dataclass(frozen=True)
class GraphNode:
    label: str
    key: dict
    properties: dict


@dataclass(frozen=True)
class GraphRelationship:
    type: str
    start: GraphNode
    end: GraphNode
    properties: dict


@dataclass(frozen=True)
class GraphPath:
    nodes: list[GraphNode]
    relationships: list[GraphRelationship]
    length: int


@dataclass(frozen=True)
class GraphResult:
    records: list[dict]                       # Cypher columns
    paths: list[GraphPath] = []
    summary: GraphResultSummary
```

### Integration with the RAG Engine

`RAGEngine.retrieve_hybrid` (ADR-0021) calls `GraphEngine.execute` with the `graph_query` argument. Graph results are converted to `RetrievedSnippet` instances:

```python
def _graph_to_snippet(node_or_path) -> RetrievedSnippet:
    return RetrievedSnippet(
        source_kind=RAGSourceKind.GRAPH,
        source_id=...,
        content=_format_for_llm(node_or_path),
        metadata={"label": ..., "key": ..., "path_length": ...},
        similarity_score=1.0,                  # graph results don't have similarity; treated as exact
        rerank_score=None,
        final_score=...,                       # set by reranker
        provenance=ProvenanceTag(
            label=f"graph: {node_or_path.label}",
            href=f"neo4j://{node_or_path.label}/{...key...}",
            timestamp=None,
        ),
    )
```

The reranker then mixes graph results into the final ordering.

### Neo4j connection management

`neo4j` Python driver, async API:

```python
class Neo4jPool:
    def __init__(self, uri: str, auth: tuple, max_connection_pool_size: int = 50): ...
    async def session(self, database: str = "neo4j") -> AsyncSession: ...
    async def close(self) -> None: ...
```

Pool: default 50 connections. Per-call session.

### Schema migrations

Migrations under `migrations/neo4j/` are Cypher scripts:

```
migrations/neo4j/
├── 0001_constraints_indexes.cypher
├── 0002_ticket_workflow_indexes.cypher
└── ...
```

`ai-coding migrate up --target=neo4j` runs them in order. Each script is run in a single transaction; constraint creation is `IF NOT EXISTS`.

Tracking applied migrations: a special `(:Migration {version: ...})` node carries the high-water mark.

### Local deployment

Shipped in `docker-compose.yml` (ADR-0019):

```yaml
neo4j:
  image: neo4j:5
  environment:
    NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:?required}
    NEO4J_PLUGINS: '["apoc"]'
  ports:
    - "127.0.0.1:7474:7474"
    - "127.0.0.1:7687:7687"
  volumes:
    - ./.local-data/neo4j/data:/data
    - ./.local-data/neo4j/logs:/logs
```

APOC plugin included for graph algorithms; we use `apoc.path.subgraphAll` and a few utilities.

### Optional: disable Neo4j

`STORAGE_ENABLE_NEO4J=false` skips:

- Neo4j connection at startup
- Outbox sync worker (rows accumulate; can be replayed later)
- Hybrid retrieval falls back to vector-only

Graph-dependent CLI commands error clearly: "Graph features disabled; set STORAGE_ENABLE_NEO4J=true and run ai-coding migrate up --target=neo4j."

### Performance + sizing

For a single developer:

- Typical Neo4j data size: ~50 MB after 6 months of usage on one repo.
- Memory: 512 MB heap (`NEO4J_server_memory_heap_max__size=512M`); v0.2 default suffices.
- Disk: SSD strongly recommended; SATA HDD makes `:IMPORTS|CALLS*1..3` traversals visibly slow.

Performance targets:

- Single-hop neighbors query: < 50ms P95
- 3-hop impact analysis on a 1000-module repo: < 500ms P95
- Outbox sync lag (PG write → Neo4j visible): < 2 seconds P95

### CLI commands

```
ai-coding graph status                    # connection health, node + edge counts per label/type
ai-coding graph query "<cypher>"          # debug Cypher (read-only enforced)
ai-coding graph impact <module-path>      # convenience for ImpactQuery
ai-coding graph chain <epic-jira-key>     # convenience for CrossProjectChainQuery
ai-coding graph reindex                   # rebuild graph from PG outbox replay
ai-coding migrate up --target=neo4j
```

### Failure handling

| Failure | Behavior |
|---|---|
| Neo4j unreachable at daemon startup | If `enable_neo4j=true` → exit 3 with hint; else continue with vector-only retrieval |
| Outbox sync worker fails mid-batch | Rows un-applied; retry on next loop; if same row fails 10 times → log error, mark `apply_failed_at`, skip; manual fix via `graph reindex` |
| Cypher query times out | `GraphTimeoutError` (Retryable); hybrid retrieval falls back to vector-only with `partial_results=true` flag |
| Constraint violation on upsert (logic bug) | Logged as ERROR; sync worker continues with next outbox row |
| `STORAGE_ENABLE_NEO4J=false` with graph-only query | Clear error: "graph disabled; vector-only fallback returned" |

## Consequences

- PostgreSQL stays the source of truth; Neo4j is a derived view consistent within ~2 seconds.
- The outbox-in-same-transaction pattern eliminates dual-write inconsistency.
- Graph queries cover the use cases (impact analysis, cross-project chains, contract consumers) that vector search can't, complementing the RAG Engine.
- Single-flag disable (`STORAGE_ENABLE_NEO4J=false`) lets v0.2 ship without forcing graph dependency on every developer.
- Code-graph indexing piggybacks on `rag index`, avoiding a parallel indexer.
- Cypher migrations live alongside SQL migrations under `migrations/`, run by the same `ai-coding migrate` command.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | When to switch outbox polling → `LISTEN/NOTIFY` for lower lag (post-v0.2 hosted scenarios) | Post-v0.2 |
| Q2 | Whether to write a small expression DSL for `GraphQuery` instead of using raw Cypher subclasses | Phase 5 implementation; only if queries proliferate |
| Q3 | How to handle Neo4j Community Edition limitations (no clustering) for hosted post-v0.2 | Post-v0.2 |
| Q4 | Memory entries embedding — should Episodic memory_entries also produce `:MemoryEvent` nodes for graph traversal? | Phase 5 design call |

## References

- ADR-0001 System Overview (S3: PG source of truth, Neo4j is view)
- ADR-0006 Multi-project + cross-project routing (`:AFFECTS_REPO`)
- ADR-0019 Storage Layer (`neo4j_outbox` table; docker-compose)
- ADR-0020 Memory Store four-layer
- ADR-0021 RAG Engine (`retrieve_hybrid`)
- ADR-0017 Error handling taxonomy (`StorageNeo4jUnavailable`, `GraphTimeoutError`)

## Reviewers

- [ ] Taven
