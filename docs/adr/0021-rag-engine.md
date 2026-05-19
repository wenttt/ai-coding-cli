# ADR-0021: RAG Engine

## Status

Proposed

## Date

2026-05-19

## Context

Specify the retrieval-augmented generation subsystem: indexable source kinds, embedding model + interface, chunking, vector retrieval, filtering, reranking, hybrid retrieval (vector + graph + memory), caching, retrieval API.

Graph retrieval against Neo4j is detailed in ADR-0022. This ADR focuses on vector + how Graph results are composed in.

## Decision

### Indexable source kinds

```python
class RAGSourceKind(StrEnum):
    OPERATION_LOG       = "operation_log"       # body_summary of each operation log
    MEMORY_EPISODIC     = "memory_episodic"     # Episodic MemoryEntry values
    MEMORY_SEMANTIC     = "memory_semantic"     # Semantic MemoryEntry values
    DESIGN_ISSUE        = "design_issue"        # GitHub design Issue bodies
    CODE_FILE           = "code_file"           # chunks of repo files
    JIRA_TICKET         = "jira_ticket"         # ticket summary + description
    CONVENTIONS_DOC     = "conventions_doc"     # workspace conventions.md
```

Each source has its own indexer (responsible for chunking + writing to `rag_chunks` and/or referencing rows in `memory_entries.embedding` / `operation_logs.body_embedding`).

### Where embeddings live

To avoid duplicating content:

| Source | Embedding column | Reason |
|---|---|---|
| Operation logs | `operation_logs.body_embedding` | body summary is small + 1:1 with the row |
| Memory entries | `memory_entries.embedding` | same 1:1 relationship |
| All other sources | `rag_chunks.embedding` | many-to-one to the source row; needs chunking |

`rag_chunks` schema is in ADR-0019. The RAG Engine treats all three tables uniformly via a `RAGCorpus` abstraction.

### Embedding model

Single configurable model, default `text-embedding-3-small` (1536 dims).

```python
class EmbeddingProvider(Protocol):
    model_name: str
    dimensions: int
    max_input_tokens: int
    request_timeout_seconds: float

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_one(self, text: str) -> list[float]: ...
```

Shipped implementations:

1. **OpenAIEmbeddingProvider** — `text-embedding-3-small` (1536) / `text-embedding-3-large` (3072). Uses the OpenAI SDK pointed at any compatible endpoint.
2. **MockEmbeddingProvider** — deterministic hash-based embedding for tests; same `dimensions` so DB schema stays valid.

Configuration in `.env`:

```
RAG_EMBEDDING__KIND=openai-compat
RAG_EMBEDDING__BASE_URL=https://llm.company.com/v1
RAG_EMBEDDING__API_KEY=...
RAG_EMBEDDING__MODEL_NAME=text-embedding-3-small
RAG_EMBEDDING__BATCH_SIZE=100
```

Model change is a schema-affecting decision: changing dimensions requires a migration that recreates the embedding columns + indexes and re-embeds all content. The CLI provides `ai-coding rag reembed --confirm` for this.

### Chunking strategy

Different sources use different chunkers:

| Source | Chunker | Max chunk tokens |
|---|---|---|
| `operation_log` body summary | as-is (already short, ≤500 chars per ADR-0005) | n/a |
| Memory entry value_json | as-is (typically small structured payloads) | n/a |
| `design_issue` | section-by-H2 (each section becomes a chunk) | 1500 |
| `code_file` (Python) | tree-sitter parse → one chunk per top-level definition (function / class / module-level code) | 1500 |
| `code_file` (TypeScript / Go / Java / ...) | language-specific tree-sitter parsers; fallback to fixed-window | 1500 |
| `code_file` (unsupported language) | fixed window (1000 chars, 200 overlap) | n/a |
| `jira_ticket` | summary + description as one chunk, plus each comment as its own | 1500 |
| `conventions_doc` | section-by-H2 | 1500 |

Chunks carry source-specific metadata in `rag_chunks.metadata` JSONB:

```python
# code_file metadata
{
    "file_path": "src/auth/login.py",
    "language": "python",
    "definition_name": "OAuthHandler.refresh",
    "definition_kind": "method",
    "line_start": 47,
    "line_end": 89,
    "imports": ["httpx", "datetime", "..."]
}

# design_issue metadata
{
    "jira_key": "PROJ-123",
    "issue_number": 45,
    "section_title": "Acceptance criteria",
    "mode": "brownfield"
}
```

Metadata is queryable; the GIN index on `rag_chunks.metadata` (ADR-0019) supports `metadata @> '{"language": "python"}'` filtering.

### Indexing flow

Indexers run reactively + on schedule:

| Source | Trigger |
|---|---|
| `operation_log` | every `OperationLogWriter.write` — embedding generated asynchronously and stored in-row |
| `memory_episodic` / `memory_semantic` | every `MemoryWriter.write` — same async pattern |
| `design_issue` | on creation + on update (ADR-0004's `create_design_issue` / `update_design_issue`) |
| `code_file` | on `ai-coding rag index` (manual) + after `git_commit` in dev workflow (opt-in via daemon config) |
| `jira_ticket` | on first read (cached) + on `ai-coding rag refresh-jira` |
| `conventions_doc` | on workspace open + when the file changes |

Embedding work is debounced + batched:

```python
class EmbeddingWorker:
    async def enqueue(self, texts: list[EmbedRequest]) -> None: ...
    async def run(self) -> None:
        while True:
            batch = await self.queue.get_batch(max_size=100, timeout=5.0)
            embeddings = await self.provider.embed([r.text for r in batch])
            await self.persist(batch, embeddings)
```

Background worker, owned by the daemon. The CLI one-shot mode embeds synchronously (no daemon).

### Retrieval API

```python
class RAGEngine(Protocol):
    async def retrieve(
        self,
        *,
        query: str,
        scope: RetrievalScope,
        sources: list[RAGSourceKind] | None = None,
        limit: int = 10,
        min_score: float = 0.5,
        rerank: bool = True,
    ) -> list[RetrievedSnippet]: ...

    async def retrieve_hybrid(
        self,
        *,
        query: str,
        scope: RetrievalScope,
        graph_query: GraphQuery | None = None,
        limit_per_source: int = 5,
        limit_total: int = 15,
    ) -> list[RetrievedSnippet]:
        """Vector retrieval across sources + Graph traversal via Neo4j (ADR-0022),
        merged + reranked."""


@dataclass(frozen=True)
class RetrievalScope:
    session_id: UUID | None
    jira_key: str | None
    project_key: str | None
    workspace_root: Path | None
    user_id: str | None


@dataclass(frozen=True)
class RetrievedSnippet:
    source_kind: RAGSourceKind
    source_id: str                   # operation_log_id / memory_entry_id / rag_chunk_id / etc.
    content: str
    metadata: dict[str, Any]
    similarity_score: float          # cosine similarity, raw
    rerank_score: float | None       # post-rerank, if rerank enabled
    final_score: float               # the score callers use for ordering
    provenance: ProvenanceTag        # for the Context Layer's [RAG: ...] tag


@dataclass(frozen=True)
class ProvenanceTag:
    label: str                       # e.g. "operation_log: PROJ-67 impl-v1"
    href: str                        # URL or file path
    timestamp: datetime | None       # of the source content
```

### Scope filtering

`RetrievalScope` populates SQL `WHERE` clauses before vector search runs. Vector search over an unfiltered table is slow + irrelevant; scope-filtered first then vector second is the default flow.

Scope rules per source:

| Source | Default filter |
|---|---|
| `operation_log` | `jira_key = ?` if provided, else recent N |
| `memory_episodic` | `session_id = ? OR jira_key = ?` |
| `memory_semantic` | `scope_project_key = ?` if provided |
| `design_issue` | issues for the given project's repos |
| `code_file` | chunks under `workspace_root` |
| `jira_ticket` | tickets in the user's project list |
| `conventions_doc` | the workspace's conventions doc |

### Reranking

After initial vector retrieval (`limit * 3` candidates), apply cross-encoder reranker for the top tier when `rerank=True`:

```python
class Reranker(Protocol):
    async def rerank(
        self,
        *,
        query: str,
        candidates: list[RetrievedSnippet],
        top_k: int,
    ) -> list[RetrievedSnippet]: ...
```

Shipped impls:

1. **LLMReranker** — uses the configured LLM Adapter (typically the compaction model, cheap) with a structured prompt: "Rank these N snippets by relevance to the query. Return JSON `[{id, score}]`."
2. **NullReranker** — no-op; preserves initial similarity ordering.

`rerank_enabled` is a config flag (default True). Off-by-default per source where rerank doesn't help (e.g., `operation_log` searches that just want chronology).

### Hybrid retrieval (with Graph)

```python
async def retrieve_hybrid(query, scope, graph_query, limit_per_source, limit_total):
    # 1. Vector retrieval across requested sources (parallel)
    vec_tasks = [self._retrieve_one_source(query, scope, src, limit_per_source) for src in sources]
    vec_results = await asyncio.gather(*vec_tasks)

    # 2. Graph retrieval if graph_query supplied
    graph_results = await self.graph.execute(graph_query, scope) if graph_query else []

    # 3. Merge candidates (dedupe by source_id)
    merged = self._merge(vec_results, graph_results)

    # 4. Rerank
    top = await self.reranker.rerank(query=query, candidates=merged, top_k=limit_total)
    return top
```

GraphQuery construction is in ADR-0022. Typical patterns:

- "Find files that import or are imported by `src/auth/login.py`" → graph traversal of module dependency edges
- "Find tickets that reference Jira PROJ-123" → graph traversal of ticket linkage edges

Graph results carry their own `RetrievedSnippet` shape with `source_kind=GRAPH_*` and provenance pointing at the Neo4j path.

### Caching

Embedding cache:

- Embedding of the same text within a daemon's lifetime → cached in-memory (`functools.lru_cache(maxsize=10_000)`)
- Cache key: `(model_name, sha256(text))`
- Persistent across daemon restart: NO. Re-embedding short texts is cheap.

Retrieval cache:

- Same query + scope within a 60-second window → cached result
- Cache invalidated by writes to any of the queried sources (driven by `EventBus`)
- Cache key: `(query, scope_hash, sources, limit)`

Both caches are off when `RAG_CACHE_ENABLED=false`.

### Provenance and citations

Every `RetrievedSnippet` has a `ProvenanceTag` that the Context Layer's `inject_retrieved_context` (ADR-0010) uses to build the `[RAG: …]` system message:

```
[RAG: 3 results]
1. operation_log: PROJ-67 implement v2 (2026-04-12)
   Made a similar OAuth refresh fix; rolled back due to test-token-refresh
   flakiness; later resolved by stubbing the provider.
2. semantic memory: AUTH error code convention (conf 0.85)
   Errors in the auth module use prefix AUTH-OAUTH-{4xx}; document in
   src/auth/errno.py.
3. code chunk: src/auth/legacy/refresh.py:34-67
   Legacy refresh logic; preserved for backward compatibility (see PROJ-12).
```

The LLM is instructed in Tier 1 to refer to these results by their numeric index and inspect their content via tool calls (`read_repo_file`, `read_operation_logs`) when needed.

### CLI commands

```
ai-coding rag index [--source <kind>] [--scope <project_key>]
    # one-shot indexing or refresh

ai-coding rag query "<query>" [--source <kind>] [--scope <jira_key>] [--limit 5]
    # debug query: prints top results with scores

ai-coding rag stats
    # show chunk counts per source, index size, last reindex timestamp

ai-coding rag reembed --confirm
    # re-embed everything (after model change)
```

### Quality metrics (post-v0.2 tracking)

Recall + relevance evaluation:

- A curated `tests/rag_eval/` set of (query, expected_top_3_source_ids) pairs.
- `ai-coding rag eval` runs the set and reports recall@5 / MRR / mean rerank improvement.
- Tracked over releases; regressions block release.

v0.2 ships the harness; the curated set seeds with a small starter (10 queries). Real eval set grows in Phase 8.

### Performance targets

- Vector retrieval for `limit=10` on a single source: < 100ms (P95)
- Hybrid retrieval across 3 sources + graph: < 500ms (P95)
- Embedding one short text (≤200 tokens): < 200ms (P95, network-bound)
- Reindex of a 1000-file repo: < 5 minutes (one-shot batch)

If these regress, perf-smoke tests (ADR-0018) catch it.

### Failure handling

| Failure | Behavior |
|---|---|
| Embedding API timeout / 5xx | Retry up to 3x with backoff; if still failing, log + skip the embed (entry stays with null embedding, picked up by background re-embed job) |
| Vector search returns no results | Empty list; not an error |
| Reranker fails | Fall back to similarity-only ordering; log warning |
| Graph query fails | Return only vector results; warn in `RetrievedSnippet.metadata.partial_results=true` |
| Cache key collision (extraordinarily unlikely) | Return cached value; daemon refresh resolves |
| Dimensions mismatch (e.g., switching model without reembed) | Vector index rejects insert with clear error; daemon refuses to start until `rag reembed` runs |

## Consequences

- Every relevant source — operation logs, memory, designs, code, Jira, conventions — feeds the same retrieval API. Callers do not pick sources; they pick scopes.
- Embeddings co-located with source rows (operation_logs, memory_entries) avoid data duplication; only diverse-source content (code chunks, Issue sections) lives in `rag_chunks`.
- Reranking is opt-in per call, default on; the cheap-LLM reranker uses the compaction model so cost stays low.
- Hybrid retrieval composes vector + graph results, with the LLM seeing one unified list (numbered, with provenance) in Context Layer Tier 3.
- Embedding model change is a planned event with a re-embed CLI command, not a silent drift.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | When to switch IVFFlat → HNSW for `rag_chunks` (large index size = build time concern with IVFFlat) | Phase 8 measurement |
| Q2 | Whether to embed code chunks with a code-specific embedding model (e.g., `voyage-code-3`) instead of general-purpose | Phase 8 quality push |
| Q3 | Eval set curation process — how to grow the 10-query starter into a useful benchmark | Phase 8 |
| Q4 | Cross-language tree-sitter packaging — which parsers ship by default, which are opt-in | Phase 4 implementation |

## References

- ADR-0005 Operation Log Schema (body_summary + body_embedding)
- ADR-0010 Context Layer (inject_retrieved_context surface)
- ADR-0011 Compactor (RAG-driven selective context)
- ADR-0014 LLM Adapter (cheap-LLM reranker reuses compaction adapter)
- ADR-0015 Observability (rag.retrieved events)
- ADR-0016 Configuration management (RAGEmbeddingConfig)
- ADR-0019 Storage Layer (rag_chunks DDL)
- ADR-0020 Memory Store (embedding columns on memory_entries)
- ADR-0022 Graph DB Neo4j integration (hybrid retrieval source)

## Reviewers

- [ ] Taven
