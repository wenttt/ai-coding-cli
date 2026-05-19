# Architecture Decision Records (ADR)

This directory holds the architectural decisions for `ai-coding-cli` v0.2. Every significant decision — what we build, how we build it, what trade-offs we accept — gets its own ADR before any code is written.

This is **not optional process overhead**. It's the contract that makes "production-grade rewrite" different from "another prototype." If a decision isn't written down with rationale, alternatives considered, and consequences accepted, it didn't happen.

## Writing rules

1. **One decision per file.** Compound ADRs hide complexity.
2. **Filename**: `NNNN-kebab-case-title.md`, NNNN is a zero-padded 4-digit sequence number.
3. **Status lifecycle**: `Proposed` → `Accepted` → (eventually) `Deprecated` or `Superseded by ADR-XXXX`.
4. **Don't edit accepted ADRs.** If a decision changes, write a new ADR that supersedes the old one. The old one stays in place as history.
5. **Write what the system is and how it works, not what it isn't.** Don't include "Scope OUT", "Anti-goals", "Alternatives Considered", or "Why not X" sections. State the design directly. Rejected options can be discussed in conversation; they don't belong in the document.
6. **Use the template.** See [`0000-adr-template.md`](./0000-adr-template.md).

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
| [0008](./0008-session-and-conversation-model.md) | Session + Conversation model | Proposed | 2026-05-19 | 0b |
| [0009](./0009-agent-core.md) | Agent Core (ReAct loop, lifecycle hooks, error model) | Proposed | 2026-05-19 | 0b |
| [0010](./0010-context-layer-three-tier.md) | Context Layer three-tier design | Proposed | 2026-05-19 | 0b |
| 0011 | Compactor (MicroCompact / AutoCompact) | _planned_ | | 0b |
| 0012 | Skill Loader (discovery + dynamic injection) | _planned_ | | 0b |
| 0013 | Tool Registry (Native + MCP bridge) | _planned_ | | 0b |
| 0014 | LLM Adapter (OpenAI-compat + Mock + provider abstraction) | _planned_ | | 0b |
| 0015 | Observability (event system, logging, metrics) | _planned_ | | 0b |
| 0016 | Configuration management (Pydantic-Settings) | _planned_ | | 0b |
| 0017 | Error handling taxonomy (Retryable / Fatal / Abort) | _planned_ | | 0b |
| 0018 | Testing strategy (mock LLM, unit, integration, E2E) | _planned_ | | 0b |
| 0019 | Storage Layer selection (PostgreSQL + pgvector) | _planned_ | | 0c |
| 0020 | Memory Store four-layer architecture | _planned_ | | 0c |
| 0021 | RAG Engine (vector retrieval, hybrid search) | _planned_ | | 0c |
| 0022 | Graph DB (Neo4j) integration + sync with PostgreSQL | _planned_ | | 0c |
| 0023 | Memory Governance (write filter, confidence, conflict, stale) | _planned_ | | 0c |
| 0024 | Grounding + Hallucination prevention | _planned_ | | 0c |
| 0025 | Guardrail Layer (Input / Output / Action) | _planned_ | | 0c |
| 0026 | Web Dashboard surface (FastAPI + frontend choice, read-only API) | _planned_ | | 0c |
| 0027 | Daemon lifecycle + CLI mode toggle (one-shot vs daemon-delegate) | _planned_ | | 0c |
| [0028](./0028-jira-workflow-specification.md) | Jira workflow specification (7-status reference workflow) | Accepted | 2026-05-19 | 0a |
| [0029](./0029-jira-reaction-mechanism.md) | Jira reaction mechanism (webhook + polling) | Accepted | 2026-05-19 | 0a |

## Phase 0 sub-phases

- **0a**: Top-level + business pipeline ADRs (0001-0007)
- **0b**: Foundation layer ADRs (0008-0018)
- **0c**: Memory / Storage / Guardrail ADRs (0019-0025)

Each ADR is reviewed and accepted before the next one is written, unless explicitly batched.

## Review gates

A Phase 0 sub-phase is complete only when **every ADR in it is Accepted**. No code is written for downstream phases until the relevant ADRs are accepted.
