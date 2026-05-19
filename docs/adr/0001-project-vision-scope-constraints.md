# ADR-0001: System Overview

## Status

Proposed (rev 3)

## Date

2026-05-19

## Context

The author's company environment has a locked-down VS Code fork, no third-party IDE extension support, a custom internal Copilot that does not speak MCP, a self-hosted GitHub Enterprise Server, a self-hosted Jira Server, and an internal OpenAI-compatible LLM gateway. Developers need a way to drive their pipeline work (Jira ticket → design → code → review → test → deploy) with AI assistance while remaining inside these constraints.

This ADR defines the system at a high level. Subsequent ADRs decompose each subsystem.

## System Overview

`ai-coding-cli` is a self-contained AI Coding Agent that runs locally on each developer's machine and drives software development from a Jira ticket through deployment. It is a Python application built on a ReAct loop over an OpenAI-compatible LLM endpoint, with first-class Memory, three-tier Context, on-demand Skill loading, and three-layer Guardrails.

The system exposes two surfaces in v0.2: a CLI for issuing commands and a local Web Dashboard for monitoring and audit. Both run on `127.0.0.1` on the developer's own machine. State is persisted to a local PostgreSQL (with `pgvector`) and Neo4j instance. Each developer's data lives only on their own machine.

The pipeline operates one stage per invocation. The developer summons the agent (via CLI or daemon), the agent reads the current state from Jira and GitHub, executes one stage, writes an operation log, and stops. The human reviews the result and re-invokes for the next stage.

The architecture splits cleanly into two layers:

- **Foundation Layer** provides the generic Agent runtime: Session/Conversation manager, Agent Core with ReAct loop, Context Layer, Compactor, Memory Store, Skill Loader, Tool Registry, Guardrail Layer, LLM Adapter, Storage Layer.
- **Application Layer** is the business pipeline implementation that runs on the Foundation: the 6 stages, state inference, retry/escalation engine, brownfield/greenfield branching, cross-project handling, multi-project routing, operation log schema, template library.

The same Foundation can host other applications in the future; v0.2 ships with the AI Coding Workflow pipeline as the one application.

## Core Capabilities

### Pipeline Stages

The system orchestrates six stages, each with explicit human review gates. The pipeline is **pull-based** (the agent reads source-of-truth state — Jira ticket status, GitHub Issue/PR state, local operation logs — on each invocation to determine where it is) and **one-step-per-invocation** (the agent completes one stage, logs it, and stops).

| Stage | Output | Review gate |
|---|---|---|
| 1. Design | A GitHub Issue containing the design markdown (YAML frontmatter + body) | Reviewers comment on the Issue; close with `completed` to approve |
| 2. Implementation | A `feat/{KEY}-...` branch with code + a code PR linked to the design Issue | Standard PR review |
| 3. Self-Review | A 6-pass review report on the diff before opening the code PR | Agent decides whether to open PR or fix Sev-1/2 findings first |
| 4. Test | Test files written + test suite run; failures trigger automatic fix-retry (up to 3 times) | Test results in operation log |
| 5. Deploy | The project's existing deploy mechanism is triggered; new env vars surfaced | Manual or CI confirmation |
| 6. Doc Update + Close | README / ARCHITECTURE / CHANGELOG updated; Jira ticket moved to Done | Documentation PR review |

Stage 1 is Issue-driven (no branches). Stage 2 onward uses branches and PRs. A 3-strike retry-then-escalate policy applies per stage; after the third failure within a stage, the agent stops and produces an `ESCALATED` operation log requesting human intervention.

### Jira Operations

The system interacts with Jira through the Atlassian Server / Cloud REST API and supports:

- **Read** — fetch a single ticket; list a user's tickets across all projects (with project routing metadata attached: which repo, which workspace, whether the current workspace matches).
- **Create** — create new Jira tickets programmatically, including:
  - Standalone tasks / stories / bugs
  - Sub-tasks under a parent ticket (used during Epic decomposition)
  - Cross-project sub-tasks for multi-repo features (auto-created from a cross-project design's `affected_projects` matrix)
- **Update** — modify summary, description, labels, assignee, priority, components, and custom fields.
- **State transitions** — move a ticket through its workflow (e.g., `In Progress` → `In Review` → `Done`).
- **Comments** — post comments, including escalation notifications that @-mention the right humans.
- **Linking** — link tickets to each other (blocks / is blocked by / relates to / is parent of).

Jira state is the source of truth for ticket lifecycle; the system reads it on every invocation rather than caching it.

### GitHub Operations

The system interacts with GHES (configurable for github.com) through the v3 REST API:

- **Issues** — create, update, read, list comments, close (with state_reason: `completed` for approved designs or `not_planned` for rejected designs).
- **Pull Requests** — create, update, read state, list review comments, list check runs, find PR by branch.
- **Branches** — create from a base, push commits.
- **Repository content** — read files via API (for cross-repo design context).

### Agent Runtime (Foundation Layer)

The Foundation provides generic Agent capabilities reusable across applications:

- **ReAct loop** — bounded by a step budget; supports parallel and sequential tool dispatch; emits lifecycle events for memory / guardrail subscribers.
- **Session + Conversation model** — sessions persist across CLI invocations; conversations are scoped multi-turn exchanges within a session.
- **Three-tier Context** — System Prompt (fixed), Static Prefix (stable per project / per user), Dynamic Context (per task). Ordered to be Prompt-Cache friendly.
- **Compactor** — MicroCompact (incremental, every N steps, removes redundant tool results) + AutoCompact (global, triggered when context approaches the model window limit, preserves task semantics + key execution facts).
- **Four-layer Memory** — Short-term (within a conversation), Working (cross-conversation within a session), Episodic (cross-session events: operation logs, ticket histories), Semantic (extracted structured knowledge: project conventions, recurring patterns).
- **Skill Loader** — Skills are discovered at three levels (user-level, project-level, Claude Code-compatible). A `load_skill` tool injects skill content on demand to keep base context small.
- **Tool Registry** — Native Python tools (Jira / GitHub / git / filesystem / shell / tests / project state inference) registered statically; optional MCP bridge for external MCP servers.
- **Three-layer Guardrails** — Input Guardrail (prompt injection detection on incoming user text and tool results), Output Guardrail (Review-Before-Write for high-risk outputs), Action Guardrail (Human-in-the-Loop confirmation for destructive operations like `git push --force`, schema migrations, deploys).
- **LLM Adapter** — provider-agnostic interface; ships with adapters for OpenAI-compatible endpoints (covering the company's internal gateway, OpenAI, and Anthropic via OpenAI-compat shim), plus a Mock adapter for testing.

### Memory & Retrieval

The system maintains compound memory that improves over time:

- **Memory Governance** — writes pass through a filter that distinguishes grounded facts (sourced from tool calls) from agent self-claims; each memory carries a confidence score and source tag. Conflicts between new and existing memories trigger a review rather than silent overwrite. Aged memories are downweighted and re-grounded before reuse.
- **Hybrid retrieval** — vector search (pgvector) for semantically similar designs, tickets, and discussions; graph traversal (Neo4j) for relational queries (module dependencies, ticket linkage chains, cross-project effects, SOP knowledge graphs).
- **Read-after-write verification** — any agent claim of having modified a file or external resource is verified by a follow-up tool read in the same session.

### Storage Layer

- **PostgreSQL** — structured data (sessions, conversations, operation logs, retry counters, skill metadata, configuration) and vector embeddings (`pgvector` extension).
- **Neo4j** — graph relationships (Jira ticket → repo → module → API endpoint; design ticket lineage; SOP knowledge graphs). Treated as a graph view over the PostgreSQL source of truth; synchronization uses an outbox pattern + CDC.
- **Local file system** — design documents and operation logs remain on disk as Markdown for git-friendly diffing; PostgreSQL holds the indexed metadata pointing at them.

## Product Surface

### CLI (primary)

The CLI is the entry point for every action.

```bash
ai-coding chat "start working on KAN-4"     # run one pipeline step
ai-coding pipeline status KAN-4              # inspect current stage + retry count
ai-coding tickets list                       # list my Jira tickets across all projects
ai-coding skills list                        # show installed skills
ai-coding daemon start | stop | status       # manage local daemon
ai-coding web                                # start daemon + open dashboard
ai-coding version
```

The CLI runs in either one-shot mode (a standalone Python process per command) or daemon-delegate mode (forwards the command to a running local daemon for faster startup and shared session state). The CLI returns non-zero exit codes on error so it composes with scripts and CI.

### Local Web Dashboard (secondary, read-only)

`ai-coding web` starts a FastAPI server on `127.0.0.1:8080` (configurable) and opens the browser. The Dashboard is a monitoring + audit surface:

- All tickets currently in flight with their pipeline stage
- Operation log timelines per ticket (visual)
- Memory store contents (filterable, searchable)
- RAG retrievals and Graph traversal results
- Token consumption trends per stage / per ticket
- Escalated tickets awaiting human intervention
- Cross-project ticket linkages visualized as a small graph
- Skill registry and which skills are loaded into which session

The Dashboard renders the system's state; it does not accept commands. Instructions go through the CLI.

### Deployment

Each developer runs their own local instance:

1. `pip install ai-coding-cli`
2. `docker compose up -d` (bundled compose file starts PostgreSQL + Neo4j locally)
3. Configure `.env` with the company LLM endpoint, Jira PAT, GHES PAT, and workspace paths
4. `ai-coding daemon start` + `ai-coding web`

Data isolation is automatic — each developer's state lives only on their own machine. The local daemon's HTTP API is the same API a future centralized deployment would expose, which preserves the upgrade path to a team-hosted instance when that's needed.

## Constraints

| # | Constraint |
|---|---|
| C1 | Runs on locked-down Windows corporate machines (no admin rights, restricted PowerShell, AV scanning subprocesses) |
| C2 | Cannot rely on third-party IDE extensions or marketplace installs |
| C3 | LLM access goes through the company's internal OpenAI-compatible gateway |
| C4 | GHES is the GitHub plane; self-hosted Jira Server is the issue plane |
| C5 | TLS connections may traverse a corporate proxy with a self-signed CA; Python's TLS chain must trust the corporate root CA |
| C6 | Source code does not leave the corporate network |
| C7 | The agent respects code review and security gates (no force-push, no bypassing required checks) |
| C8 | Operation logs are auditable: who, what, when, why, with what input |
| C9 | All runtime state and storage stay on the developer's machine |
| C10 | All service ports bind to `127.0.0.1` only |

## Success Criteria

### Technical

| Criterion | Target |
|---|---|
| End-to-end pipeline runs on a real Jira ticket | One ticket completes Stage 1-6 in the company environment |
| Operation logs validate against schema | 100% of stage completions produce schema-valid logs |
| LLM provider swappable | At least two providers integration-tested (company gateway + Anthropic) |
| Test coverage | ≥ 80% on Foundation; ≥ 60% on Application |
| Prompt injection resistance | Curated red-team suite passes 100% |
| Long-conversation stability | Long-running tickets retain key information across context compaction events |

### Operational

| Criterion | Target |
|---|---|
| Install on corporate Windows machine | Single command after Docker is available |
| Failure to start | Actionable error in stderr within 5 seconds |
| Dashboard startup | Renders within 3 seconds of `ai-coding web` |
| Dashboard browser support | Current Chrome / Edge / Safari |
| Daemon shutdown | Clean SIGTERM handling, no orphaned DB connections |
| Configuration | All credentials in a single `.env` file |

## Open Questions

Resolved in subsequent ADRs; listed here for traceability:

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Reliability of the company LLM's tool-calling support | ADR-0014 |
| Q2 | PostgreSQL availability on corporate workstations (IT approval, packaging) | ADR-0019 |
| Q3 | Practicality of Neo4j on a personal workstation (resource footprint, packaging) | ADR-0022 |
| Q4 | How Skills are shared across team members | ADR-0012 |
| Q5 | Platform support — Windows-first vs Windows + Linux + macOS equally | ADR-0002 |
| Q6 | Public open-source aspiration vs purely internal | resolved by license (MIT) and project README |
| Q7 | Packaging of PostgreSQL + Neo4j for local deployment | ADR-0019 + ADR-0022 |
| Q8 | Daemon lifecycle across platforms (systemd / Windows Service / launchd / CLI-managed) | ADR-0027 |
| Q9 | Web Dashboard frontend stack — HTMX + Tailwind vs minimal React | ADR-0026 |
| Q10 | Localhost auth threat model — light auth for multi-user OS machines | revisit when threat model is detailed |

## References

- v0.1 prototype tagged at [`v0.1-prototype-archive`](https://github.com/wenttt/ai-coding-cli/tree/v0.1-prototype-archive)
- Business pipeline reference: [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow)

## Reviewers

- [ ] Taven
