# ADR-0004: Stage 1 Design Flow

## Status

Accepted

## Date

2026-05-19

## Context

Specify how `DesignStageHandler` produces a design Issue from a Jira ticket — including the brownfield / greenfield / cross-project branches, the design Issue structure, the YAML frontmatter schema downstream stages consume, and the rework flow.

## Decision

### DesignStageHandler dispatch

`DesignStageHandler` is the entry handler for status `DESIGN_DRAFTING`. It dispatches to one of three sub-handlers:

```
DesignStageHandler
    ├── if ctx.is_cross_project → CrossProjectDesignHandler
    ├── elif ctx.mode == "greenfield" → GreenfieldDesignHandler
    └── else → BrownfieldDesignHandler
```

`ctx.is_cross_project` and `ctx.mode` are populated by `PipelineOrchestrator._build_context` (ADR-0003).

### Brownfield design flow

`BrownfieldDesignHandler.run(ctx)`:

1. **Read existing design Issue (idempotency)**: `find_design_issue_for_jira(jira_key)`. If an open Issue exists, abort — the orchestrator should not have routed here.
2. **Read Jira ticket**: already in `ctx.jira_ticket`.
3. **Select template**: by `ticket.issuetype` (`Story` → `user_story`, `Task` → `task`, `Sub-task` → `sub_task`, `Epic` → `epic`). Load `templates/brownfield/{name}.md`.
4. **Search repo for context**: extract 3-7 concrete keywords from `summary + description` (skip generic words). Call `find_relevant_modules(keywords, limit=10)`.
5. **Read top references**: `read_repo_file` on the top 3-5 results. Cap each file at 8KB; truncate beyond.
6. **Compose design via agent**: invoke the ReAct agent with a prompt anchored on the template + ticket + retrieved code. The agent fills placeholders, drafts AC in GIVEN/WHEN/THEN form, lists affected modules.
7. **Validate frontmatter**: the result MUST parse as YAML; required fields enforced (`jira_key`, `mode`, `ticket_type`, `ac`, `affected_modules`, `risk_level`).
8. **Create GitHub Issue**: `create_github_issue(title, body, labels)`. Title format: `[{JIRA_KEY}] Design: {summary}`. Labels: `jira:{key_lower}`, `stage:design`, `mode:brownfield`, plus `ticket-type:{type}`.
9. **Link Jira ↔ Issue**: set Jira custom field `Design Issue` to the Issue URL; post a Jira comment with the link.
10. **Return `StageResult`**: outcome `completed`, artifacts `{"design_issue_url": ..., "design_issue_number": ...}`.

### Greenfield design flow

`GreenfieldDesignHandler.run(ctx)` differs from brownfield in steps 3-6:

3. **Select template**: based on inferred scope (`new_project` if workspace is empty; `new_service` if Jira labels `service`; default `new_project`). Templates live at `templates/greenfield/`.
4. **Read related tickets** (not repo): the brownfield "search repo" step has no equivalent — the repo is empty. Instead, fetch all `linked_issues` from the ticket and read their summaries + descriptions for additional context.
5. (no separate retrieval step)
6. **Compose design**: agent fills the template's stack-decision matrix (language / framework / DB / cache / API style / auth / deployment / CI/CD / testing / observability). Each row gets a recommended choice + alternatives + rationale.
7. **Validate frontmatter**: required fields include `proposed_stack` (Pydantic-validated dict) in addition to the common set.
8. Same `create_github_issue` step. Add `mode:greenfield` label.

### Cross-project design flow

`CrossProjectDesignHandler.run(ctx)`:

1-4. Same as brownfield (cross-project tickets are typically Epic-level on an existing repo).
5. **Identify affected repos**: from `ctx.affected_projects` (already computed). Each `ProjectRouting` has `{project_key, repo, workspace_path, role_hint}`.
6. **Read context across affected repos**: for each repo in `affected_projects`, run `find_relevant_modules` with the same keywords. Aggregate top 3 per repo, capped at 10 total.
7. **Compose design with Contract section**: use `templates/brownfield/cross_project.md`. The agent MUST fill:
   - `frontmatter.is_cross_project: true`
   - `frontmatter.affected_projects: [...]`
   - `frontmatter.implementation_order: [...]` (default: backend first, frontend second; agent may override with rationale)
   - `frontmatter.contract: {type, source_of_truth_path, api_endpoints}`
   - Body section "Contract" with concrete schemas (OpenAPI / Protobuf / GraphQL) — the agent SHOULD pick the project's convention by inspecting `contracts/` directories across affected repos.
8. **Validate Contract section**: parse the embedded schemas (yaml.safe_load for OpenAPI; protoc syntax check for Protobuf via subprocess; graphql parser for GraphQL). If parse fails → outcome `failed`, no Issue opened.
9. **Create design Issue in primary repo**: the repo from `affected_projects[0]` is the primary. Issue labels add `cross-project` + `affected-repos:{...}`.
10. **Add Jira label**: `cross-project`.

The handler does NOT yet create sub-tickets in affected projects. Sub-tickets are created after design approval, by the orchestrator on the `DESIGN_REVIEW → IN_DEVELOPMENT` transition (see "Sub-ticket fan-out" below).

### Design Issue structure

Issue body:

```markdown
---
jira_key: PROJ-123
jira_url: https://jira.company.com/browse/PROJ-123
mode: brownfield
ticket_type: user_story
is_cross_project: false
risk_level: medium
ac:
  - "User can log in with OAuth provider X"
  - "Failed login surfaces a structured error"
affected_modules:
  - src/auth/
  - src/api/login.py
proposed_stack: null    # only populated in greenfield
affected_projects: []   # only populated when is_cross_project
contract: null          # only populated when is_cross_project
---

# Design: {summary}

> Jira: [{JIRA-KEY}]({jira_url})

## Background

{2-3 paragraphs: what the system does today around this feature}

## Story / Goal

{as / want / so that — for user_story; problem statement for task / epic}

## Acceptance criteria

{GIVEN / WHEN / THEN; mirrors `ac` in frontmatter}

## Affected modules

{narrative; mirrors `affected_modules` in frontmatter}

## Design

{the actual technical design — diagrams, data flow, API shape, persistence,
edge cases, error handling}

## Test plan

{what should be tested at Stage 4; brief}

## Open questions

{anything that requires reviewer input}

---

> Generated by ai-coding-cli at {timestamp}. Review by transitioning the
> Jira ticket to DESIGN_APPROVED or DESIGN_REWORK.
```

The frontmatter is the machine-readable contract. The body is for human reviewers. Both stay in sync; the handler ensures consistency.

### Cross-project Issue body additions

Cross-project Issues include three additional sections, placed before "## Design":

```markdown
## Affected projects

| Project | Repo | Role |
|---|---|---|
| BACKEND | company/api-backend | service implementation |
| FRONTEND | company/web-frontend | UI integration |

## Implementation order

1. backend (defines the contract)
2. frontend (consumes the contract)

## Contract

> Source of truth: `contracts/PROJ-123.yaml` in the backend repo.

\`\`\`yaml
openapi: 3.0.3
paths:
  /api/v1/...:
    post:
      requestBody: ...
      responses:
        200: ...
\`\`\`

(error codes, versioning rules, examples)
```

### Frontmatter schema

Pydantic model (in `application/pipeline/stages/design/frontmatter.py`):

```python
class DesignFrontmatter(BaseModel):
    jira_key: str
    jira_url: HttpUrl
    mode: Literal["brownfield", "greenfield"]
    ticket_type: Literal["user_story", "task", "sub_task", "epic"]
    is_cross_project: bool = False
    risk_level: Literal["low", "medium", "high"]
    ac: list[str]                                # acceptance criteria
    affected_modules: list[str] = []
    proposed_stack: ProposedStack | None = None  # greenfield only
    affected_projects: list[ProjectRouting] = [] # cross-project only
    implementation_order: list[str] | None = None
    contract: ContractSpec | None = None         # cross-project only
```

Downstream stages (2, 3, 4) parse this frontmatter to decide their behavior. The handler enforces the schema before creating the Issue; an invalid frontmatter is a `RetryableError` (the next attempt re-generates with the agent informed of the validation failure).

### Design rework flow

`DesignReworkHandler.run(ctx)` runs on entry to `DESIGN_REWORK`:

1. **Read the existing Issue**: `get_github_issue(issue_number)` from the Jira custom field.
2. **Read reviewer comments**: `list_github_issue_comments` + Jira ticket comments since the last `DESIGN_DRAFTING → DESIGN_REVIEW` transition.
3. **Read prior operation logs**: filter to this ticket + stage in `("design", "design-rework")`. Pass the most recent 3 to the agent.
4. **Diagnose feedback**: agent classifies each comment as `concrete-change` / `question` / `suggestion` / `approval-noise`. Filters to actionable items.
5. **Plan edits**: agent produces a list of edits — section-level diffs against the existing Issue body. Edits must NOT delete entire sections (preserve structure); they MAY modify text + frontmatter values.
6. **Apply edits + validate frontmatter**: same Pydantic validation as fresh design.
7. **Update Issue**: `update_github_issue(issue_number, body)` with the revised body.
8. **Post summary comment on Issue**: a structured comment listing what changed, mapped to which reviewer comment.
9. **Transition Jira**: orchestrator transitions back to `DESIGN_DRAFTING` (per ADR-0028 transition table). The status loop is `DRAFTING → REVIEW → REWORK → DRAFTING` per cycle; retry budget caps the loop at 3.

If a reviewer's comment conflicts with another reviewer's prior approval, the handler:
- Surfaces the conflict in the summary comment (`@-mentioning` both reviewers)
- Picks a path with reasoning in the body
- Records the decision in the operation log

### Sub-ticket fan-out (cross-project)

After a cross-project design Issue is approved (transition to `DESIGN_APPROVED → IN_DEVELOPMENT`), `PipelineOrchestrator` performs:

1. **For each affected project**: `JiraToolClient.create_sub_task(parent=jira_key, project=project_key, summary=f"[{role}] {original_summary}", description=link_to_design_issue, labels=["jira-sub-task", "cross-project"])`.
2. **Set parent ticket's affected_projects** custom field with the new sub-ticket keys.
3. **Initial status**: each new sub-ticket starts at `IN_DEVELOPMENT` (not TODO — design is already approved at the parent level).
4. The parent ticket also transitions to `IN_DEVELOPMENT` but with the `parent-cross-project` label; the parent's `IN_DEVELOPMENT` handler is a passive watcher that waits for all sub-tickets to reach `DONE`.

Each sub-ticket runs its own pipeline. The agent on each developer's machine reacts to its assigned sub-ticket independently.

### Idempotency

Each handler is idempotent on `(jira_key, stage, retry_count)`. Concretely:

- `BrownfieldDesignHandler.run` first calls `find_design_issue_for_jira`. If an Issue exists AND its frontmatter `retry_count` matches `ctx.retry_count`, the handler returns the existing Issue (does NOT create a duplicate).
- `DesignReworkHandler.run` looks up the existing Issue and updates body; never creates a new Issue.
- `CrossProjectDesignHandler.run` checks for both the primary Issue and any existing sub-tickets before creating new ones.

This protects against duplicate delivery from the webhook + polling channels (ADR-0029).

### Error handling

| Failure | Classification | Recovery |
|---|---|---|
| Jira ticket fetch fails | `RetryableError` (with backoff) | Orchestrator retries on next event |
| `find_relevant_modules` returns empty | Not an error; design proceeds with explicit note "no relevant modules found" |
| Template parse fails (templates are shipped artifacts; this is a bug) | `FatalError` | Escalate immediately; no retry |
| LLM returns invalid frontmatter | `RetryableError` | Agent re-runs with prior validation error in prompt |
| GitHub Issue creation fails | `RetryableError` | Orchestrator retries on next event |
| Jira custom field update fails (Issue created OK) | Logged; not blocking | Operation log records the disconnect; manual reconciliation possible |

## Consequences

- A single dispatcher handles three branches (brownfield / greenfield / cross-project) without proliferating handler classes at the orchestrator level.
- The design Issue body is the design's source of truth. Frontmatter + Markdown stay in sync per handler invariant. Downstream stages parse only the frontmatter for structured data; the body is for humans.
- Cross-project design produces a single Issue in the primary repo with the contract section; sub-ticket fan-out is deferred until design approval, avoiding scattered work-in-progress sub-tickets.
- Rework loop reuses the same Issue (updates body), preserving comment history and reviewer context.
- The retry budget enforces convergence: a design that takes more than 3 rework rounds escalates.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | OpenAPI / Protobuf / GraphQL parsing tooling — which libraries, where they're packaged | Implementation-phase decision |
| Q2 | Whether cross-project Epic completion can be enforced by the agent (vs only by Jira automation) | Operational deployment doc |
| Q3 | How `find_relevant_modules` keyword extraction handles non-English Jira tickets | Implementation; possibly LLM-extracted keywords |
| Q4 | How operation logs from rework rounds reference prior rounds (chain vs flat list) | ADR-0005 |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline Business Model (StageHandler interface)
- ADR-0005 Operation log schema (planned)
- ADR-0007 Template library (planned)
- ADR-0028 Jira Workflow Specification
- ADR-0029 Jira Reaction Mechanism

## Reviewers

- [ ] Taven
