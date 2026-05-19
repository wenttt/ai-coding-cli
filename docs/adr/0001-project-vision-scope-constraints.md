# ADR-0001: Project Vision, Scope, Constraints

## Status

Proposed

## Date

2026-05-19

## Context

We are rewriting `ai-coding-cli` to be a production-grade AI Coding Agent for use inside the author's company. The v0.1 prototype (a 1-day ReAct hello world relying on subprocess MCP and an IDE Agent) was inadequate for production: it did not address context management, memory, governance, observability, or operational concerns that a real Agent deployment requires.

Before any code is written, we must agree on what this project IS and what it ISN'T. Without this, every downstream ADR drifts and contradicts.

This ADR establishes:

1. **What the project is** — its purpose, the user, and the value delivered.
2. **What it is NOT** — explicit non-goals to prevent scope creep.
3. **The constraints we operate under** — environmental, technical, and organizational.
4. **The success criteria** — how we know if this is working.

Every subsequent ADR derives its trade-offs from this one.

## Decision

### 1. Purpose

`ai-coding-cli` is **a self-contained, production-grade AI Coding Agent** that drives the software development lifecycle from a Jira ticket through to deploy, using a ReAct loop over a configurable OpenAI-compatible LLM. It is the **runtime** for the `ai-coding-workflow` business pipeline (formerly delivered as an MCP server). The CLI is the entry point; future entry points (HTTP API, in-process library use) reuse the same core.

### 2. Target user

Primary: **the author + the author's engineering team**, working inside a restricted corporate environment with:

- A locked-down VS Code fork that does not run third-party MCP clients reliably
- A corporate "Copilot" that does not implement the MCP protocol
- A self-hosted GitHub Enterprise Server (GHES)
- A self-hosted Jira Server / Data Center
- Internal LLM gateway exposing an OpenAI-compatible endpoint
- Mandatory code review + security gates that the Agent must respect, not bypass

Secondary: **the author's portfolio / resume**. The architecture must be defensible as "principal engineer–level work."

### 3. Value delivered

For a single developer:

- One sentence in → fully orchestrated stage execution → human review gate
- Audit trail of every Agent decision (operation logs)
- Context management that survives long conversations and cross-session work
- Memory that compounds: the more the Agent works on the repo, the better it gets
- Guardrails that prevent destructive actions and prompt injection

For a team:

- Consistent design / implementation / review process across engineers
- Shared SOPs encoded as Skills, loadable on demand
- Cross-project orchestration (frontend + backend changes for one Jira ticket) with contract-first design
- 3-strike escalation: Agent stops and asks for a human after N failures

### 4. Scope — IN

The system MUST:

- Run anywhere Python 3.11+ runs, including locked-down corporate machines
- Talk to any OpenAI-compatible LLM endpoint (company gateway, OpenAI, Anthropic shim)
- Drive a 6-stage pipeline: Design → Implement → Self-Review → Test → Deploy → Doc Update
- Persist state in PostgreSQL + pgvector and Neo4j (decision deferred to ADR-0019, ADR-0022)
- Provide a CLI entry point as the v0.2 primary UX
- Be testable end-to-end without calling a real LLM (mock provider for tests)
- Produce machine-readable operation logs after every stage
- Enforce a 3-strike retry-then-escalate policy per stage
- Support brownfield + greenfield project modes
- Support cross-project (multi-repo) tickets with contract-first design
- Support multi-project routing (one developer's tickets span multiple Jira projects → multiple GHES repos)
- Implement a four-layer Memory architecture with write governance
- Implement three-layer Context with cache-friendly prefix ordering
- Implement Skill discovery + on-demand injection
- Implement input + output + action Guardrails (including Human-in-the-Loop)

### 5. Scope — OUT (explicit non-goals)

The system will NOT, in v0.2:

- Replace existing CI/CD systems. We trigger them; we don't reimplement them.
- Replace Jira or GitHub. We drive them; we don't host alternatives.
- Be a general-purpose chatbot. The system is task-oriented around the pipeline.
- Support arbitrary IDE integrations (Cursor extension, VS Code extension, JetBrains plugin). CLI is the only v0.2 surface. Other surfaces are deferred to post-v0.2.
- Train or fine-tune models. Inference only.
- Provide a web UI / dashboard. Deferred to post-v0.2.
- Support languages other than what the underlying LLM and our skills know. We don't lock to a specific stack.
- Pretend to be an autonomous agent. **The system is summon-once-runs-one-step-stops.** The human is in the loop every step.
- Support real-time collaboration (multiple humans + one agent on the same ticket simultaneously). Deferred.

### 6. Hard constraints (non-negotiable)

These are external constraints we cannot change; the architecture must accommodate them:

| # | Constraint | Source |
|---|---|---|
| C1 | Must run on locked-down Windows corporate machines (no admin, restricted PowerShell, AV scanning subprocesses) | Author's company IT |
| C2 | Cannot rely on third-party IDE extensions (custom Copilot fork, no marketplace access) | Author's company IT |
| C3 | LLM access is via internal OpenAI-compatible gateway; we do NOT pick the LLM | Author's company AI platform |
| C4 | GHES is the GitHub plane; Jira Server is the issue plane | Author's company tools |
| C5 | TLS connections may go through corporate proxy with self-signed CA; Python must trust corporate root CA | Author's company network |
| C6 | Source code may not leave the corporate boundary; any storage must be local or company-internal | Compliance |
| C7 | The Agent must not bypass code review or security gates (no force-push, no bypassing required checks) | Company policy |
| C8 | Operation logs must be auditable (who, what, when, why, with what input) | Compliance |

### 7. Soft constraints (preferred but flexible)

| # | Constraint | Rationale |
|---|---|---|
| S1 | Single language for core (Python) | Author's expertise + Python ecosystem for LLM tooling |
| S2 | Single mono-package (no v0.1 cli + workflow split) | Reduce subprocess failure points |
| S3 | All durable state in PostgreSQL (single source of truth); Neo4j is a graph view | Operational simplicity |
| S4 | One .env per deployment; no parallel configuration files | Configuration drift is a leading cause of production incidents |
| S5 | All LLM calls go through an Adapter; the rest of the codebase is provider-agnostic | Future-proofing |

### 8. Success criteria (how we know it works)

#### Technical (v0.2 must hit)

| Criterion | Target | How measured |
|---|---|---|
| End-to-end pipeline runs on a real Jira ticket | 1 ticket completes Stage 1-7 | Manual run on author's company environment |
| Operation logs are complete + machine-readable | 100% of stages emit valid logs | Schema validation in CI |
| LLM provider swappable | At least 2 providers verified (company + Anthropic) | Integration tests |
| Token consumption per ticket | < target (TBD when baseline measured) | Telemetry per run |
| Test coverage | ≥ 80% unit coverage on Foundation; ≥ 60% on Pipeline | pytest-cov |
| Hallucination guardrails effective | Prompt injection attack suite passes 100% | Curated red-team test set |
| Context overflow handling | Long-running tickets don't lose key information | Long-conversation test fixture |

#### Operational (v0.2 must hit)

- 1 command install on author's company Windows machine
- Database migrations are reversible
- All credentials in a single `.env` file; documented in `.env.example`
- Failure to start surfaces actionable error in stderr within 5 seconds
- Memory + storage usage profiled and documented

#### Strategic (v0.2 may aspirationally hit)

- Architecture defensible as "principal engineer-level work" for portfolio purposes
- At least 1 teammate other than the author uses it (would be an adoption signal)
- Code can be extracted and open-sourced if company approves

### 9. Anti-goals (things that look like success but aren't)

- **Demoability is not success.** A demo on the author's laptop with cherry-picked tickets does not count.
- **Coverage of tools/features is not success.** A long list of implemented MCP tools without a working end-to-end run is failure.
- **Aesthetic code is not success.** Clean code that doesn't run on the company environment is failure.
- **Theoretical purity is not success.** Architectural elegance that doesn't deliver Stage 1-7 on a real ticket is failure.

## Consequences

### Positive

- Every subsequent ADR has a clear reference point: "does this serve the purpose / fit the constraints / contribute to success?"
- Scope creep can be cut by referring back to Section 5 (Scope OUT).
- The Storage + Graph decisions in ADR-0019 / ADR-0022 inherit S3 (PostgreSQL as single source of truth, Neo4j as view).
- Reviewer can hold the project accountable: if v0.2 ships and the success criteria aren't met, we know.

### Negative

- The breadth (24 ADRs, 5.5 months) commits us to a long Phase 0. Any urgency forcing a faster timeline conflicts with this commitment.
- "No IDE integration" closes a real user-experience door for v0.2. Some teammates may prefer a VS Code extension over a CLI.
- "Single LLM provider via Adapter" means we don't optimize for any one LLM's specific tool-calling quirks.
- Constraint C7 (no bypassing security gates) means the Agent may stall on tickets where the human side is the bottleneck. We accept this.

### Neutral / Trade-offs

- Targeting a specific corporate environment (GHES + Jira Server + locked Windows) makes the system **less portable** in trade for being **actually deployable in the author's environment**. A non-author user would need to adapt config + connector layer.
- Neo4j commits to an additional database. The graph-related ADRs (0022) will weigh this in detail.

## Alternatives Considered

### Alternative 1: Continue the v0.1 prototype path (Roo Code + MCP server)

**Description**: Keep ai-coding-workflow as a standalone MCP server; rely on Roo Code or similar third-party VS Code extension as the Agent runtime.

**Why not chosen**:
- Roo Code installations were unreliable on the author's company VS Code fork (MCP plumbing fails silently, SSL issues, AV scans cripple subprocess startup).
- The company's "Copilot" does not speak MCP.
- Maintenance cost of a third-party Agent runtime we don't control is high — every Roo Code update could break us.
- Doesn't satisfy success criterion: "Architecture defensible as principal engineer-level work" (assembly, not engineering).

### Alternative 2: Build a VS Code extension

**Description**: Implement the Agent as a VS Code extension instead of a CLI.

**Why not chosen**:
- Hard violates C2 (the company VS Code fork's extension model is limited and unreliable).
- Even if we shipped a working extension, only the author would benefit; teammates with different IDEs would be excluded.
- A CLI is the universal substrate; extensions can be added later (Phase 9+).

### Alternative 3: Build a server-side / web-app Agent

**Description**: Run the Agent as a hosted internal service with a web UI; developers interact via browser.

**Why not chosen**:
- C6 (source code may not leave corporate boundary) makes hosting decisions politically expensive.
- Deferred operational commitment (uptime, scaling, multi-tenant security) is large.
- v0.2 needs to ship to a single user (the author) first; web-app shape is post-v0.2.

### Alternative 4: Vendor-lock to a single LLM (e.g. just Claude or just GPT-4o)

**Description**: Drop the LLM Adapter; hard-code one provider's SDK.

**Why not chosen**:
- C3 forces us to use the company's internal LLM; we can't pick.
- Strategic constraint: the company LLM may change. Adapter pattern is small (low cost), high optionality.

### Alternative 5: Do nothing — wait for company tooling to mature

**Description**: Don't build this. Wait for the company's "Copilot" or some future internal tool.

**Why not chosen**:
- Author's resume / portfolio needs a real artifact, not a wait-and-see.
- Company tooling timeline is multi-year and not within author's control.
- The pipeline problem (Stage 1-7 orchestration with audit) is not on the company's roadmap.

## Open Questions

These don't block ADR-0001 acceptance but must be answered in later ADRs:

- **Q1**: Does the company LLM expose tool-calling reliably enough? Spec says yes; needs validation in Phase 1. → ADR-0014.
- **Q2**: Is PostgreSQL allowed on author's company workstation? Will require IT approval if not bundled. → ADR-0019.
- **Q3**: Is Neo4j practical on a personal workstation, or does it need a shared deployment? → ADR-0022.
- **Q4**: How are skills shared across team members — do we ship them in this repo, or fork-per-team? → ADR-0012.
- **Q5**: Do we support Windows + Linux + macOS equally in v0.2, or Windows-first? Author's company env is Windows. → ADR-0002.
- **Q6**: Is there a public-internet open-source aspiration, or is this purely internal? Affects license + telemetry + dependency choices. → addressed by S5 + project README license.

## References

- v0.1 prototype: [`v0.1-prototype-archive`](https://github.com/wenttt/ai-coding-cli/tree/v0.1-prototype-archive)
- Pipeline reference implementation: [ai-coding-workflow](https://github.com/wenttt/ai-coding-workflow)
- The conversation that triggered this rewrite (May 2026): the prototype Agent kept producing code without going through Stage 1 design review, and the company environment blocked every IDE Agent we tried. The lesson: depending on a third-party Agent runtime is the failure mode.

## Reviewers

- [ ] Taven
