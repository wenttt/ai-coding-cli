# Architecture Decision Records (ADR)

This directory holds the architectural decisions for `ai-coding-cli` v0.2. Every significant decision — what we build, how we build it, what trade-offs we accept — gets its own ADR before any code is written.

## Writing rules

1. **One decision per file.** Compound ADRs hide complexity.
2. **Filename**: `NNNN-kebab-case-title.md`, NNNN is a zero-padded 4-digit sequence number.
3. **Status lifecycle**: `Proposed` → `Accepted` → (eventually) `Deprecated` or `Superseded by ADR-XXXX`.
4. **Don't edit accepted ADRs.** If a decision changes, write a new ADR that supersedes the old one. The old one stays in place as history.
5. **Write what the system is and how it works, not what it isn't.** Don't include "Scope OUT", "Anti-goals", "Alternatives Considered", or "Why not X" sections. State the design directly. Rejected options can be discussed in conversation; they don't belong in the document.
6. **Use the template.** See [`0000-adr-template.md`](./0000-adr-template.md).

## Status: Phase 0 complete; Lite profile defined

ADRs 0001-0029 (27 ADRs) form the Standard profile design — Accepted, reserved for v0.3+.
ADR-0030 defines the **Lite profile** that v0.2 ships: single-user, single SQLite file, single Python package. v0.2 implementation begins from Lite.

## Index

| # | Title | Status | Date | Phase |
|---|---|---|---|---|
| [0001](./0001-project-vision-scope-constraints.md) | System overview | Accepted (rev 5) | 2026-05-19 | 0a |
| [0002](./0002-repository-structure-and-package-organization.md) | Repository structure + package organization | Accepted | 2026-05-19 | 0a |
| [0003](./0003-pipeline-business-model.md) | Pipeline business model (state machine, handlers, orchestrator) | Accepted | 2026-05-19 | 0a |
| [0004](./0004-stage-1-design-flow.md) | Stage 1 design flow (brownfield / greenfield / cross-project) | Accepted | 2026-05-19 | 0a |
| [0005](./0005-operation-log-schema.md) | Operation log schema + storage + writer/reader API | Accepted | 2026-05-19 | 0a |
| [0006](./0006-multi-project-cross-project-routing.md) | Multi-project + cross-project routing | Accepted | 2026-05-19 | 0a |
| [0007](./0007-template-library.md) | Template library (brownfield / greenfield / cross_project) | Accepted | 2026-05-19 | 0a |
| [0008](./0008-session-and-conversation-model.md) | Session + Conversation model | Accepted | 2026-05-19 | 0b |
| [0009](./0009-agent-core.md) | Agent Core (ReAct loop, lifecycle hooks, error model) | Accepted | 2026-05-19 | 0b |
| [0010](./0010-context-layer-three-tier.md) | Context Layer three-tier design | Accepted | 2026-05-19 | 0b |
| [0011](./0011-compactor.md) | Compactor (MicroCompact / AutoCompact) | Accepted | 2026-05-19 | 0b |
| [0012](./0012-skill-loader.md) | Skill Loader (discovery + dynamic injection) | Accepted | 2026-05-19 | 0b |
| [0013](./0013-tool-registry.md) | Tool Registry (Native + MCP bridge) | Accepted | 2026-05-19 | 0b |
| [0014](./0014-llm-adapter.md) | LLM Adapter (OpenAI-compat + Anthropic + Mock) | Accepted | 2026-05-19 | 0b |
| [0015](./0015-observability.md) | Observability (event bus, logging, metrics, event catalog) | Accepted | 2026-05-19 | 0b |
| [0016](./0016-configuration-management.md) | Configuration management (Pydantic-Settings) | Accepted | 2026-05-19 | 0b |
| [0017](./0017-error-handling-taxonomy.md) | Error handling taxonomy (Retryable / Fatal / Abort) | Accepted | 2026-05-19 | 0b |
| [0018](./0018-testing-strategy.md) | Testing strategy (mock LLM, unit, integration, E2E) | Accepted | 2026-05-19 | 0b |
| [0019](./0019-storage-layer.md) | Storage Layer (PostgreSQL + pgvector) | Accepted | 2026-05-19 | 0c |
| [0020](./0020-memory-store-four-layer.md) | Memory Store four-layer architecture | Accepted | 2026-05-19 | 0c |
| [0021](./0021-rag-engine.md) | RAG Engine (vector + hybrid retrieval) | Accepted | 2026-05-19 | 0c |
| [0022](./0022-neo4j-graph-integration.md) | Neo4j graph integration + sync | Accepted | 2026-05-19 | 0c |
| [0023](./0023-memory-governance.md) | Memory Governance (write filter, confidence, conflict, stale) | Accepted | 2026-05-19 | 0c |
| [0024](./0024-grounding-and-hallucination-prevention.md) | Grounding + Hallucination prevention | Accepted | 2026-05-19 | 0c |
| [0025](./0025-guardrail-layer.md) | Guardrail Layer (Input / Output / Action) | Accepted | 2026-05-19 | 0c |
| [0026](./0026-web-dashboard-surface.md) | Web Dashboard surface | Accepted | 2026-05-19 | 0c |
| [0027](./0027-daemon-lifecycle.md) | Daemon lifecycle + CLI mode toggle | Accepted | 2026-05-19 | 0c |
| [0028](./0028-jira-workflow-specification.md) | Jira workflow specification (7-status reference workflow) | Accepted | 2026-05-19 | 0a |
| [0029](./0029-jira-reaction-mechanism.md) | Jira reaction mechanism (webhook + polling) | Accepted | 2026-05-19 | 0a |
| [0030](./0030-v0.2-lite-profile.md) | v0.2 Lite profile (single-user SQLite) | Proposed | 2026-05-20 | Lite |

## Phase 0 sub-phases (all complete)

- **0a — Top-level + business pipeline**: ADRs 0001-0007, 0028, 0029 (9 ADRs)
- **0b — Foundation layer**: ADRs 0008-0018 (11 ADRs)
- **0c — Memory / Storage / UX / Guardrail**: ADRs 0019-0027 (9 ADRs)

## Review gates

A Phase 0 sub-phase is complete only when **every ADR in it is Accepted**. Phase 1 (implementation) is now unblocked.
</content>
