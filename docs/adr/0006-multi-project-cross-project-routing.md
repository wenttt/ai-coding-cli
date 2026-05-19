# ADR-0006: Multi-project + Cross-project Routing

## Status

Proposed

## Date

2026-05-19

## Context

Specify how the system handles two distinct scenarios:

- **Multi-project**: one developer's Jira tickets span multiple Jira projects, each backed by a separate GitHub repo + local workspace. Each ticket lives in exactly one project.
- **Cross-project**: one Jira ticket changes touches multiple repos (e.g., backend + frontend for the same feature).

## Decision

### Project mapping configuration

A YAML file maps Jira project keys to GitHub repos and local workspace paths.

Location: `~/.config/ai-coding-cli/project_mapping.yaml` (user-level; team members each maintain their own copy with their workspace paths).

```yaml
default:
  github_owner: company
  github_default_repo: monolith       # fallback for unrecognized Jira projects
  workspace_path: ~/code/monolith

projects:
  AUTH:
    github_owner: company
    github_repo: auth-service
    workspace_path: ~/code/auth-service
    description: "Authentication service"
    default_base_branch: main
    default_reviewers: ["alice", "bob"]
    role_hint: backend                 # used by cross-project handler

  WEB:
    github_owner: company
    github_repo: web-frontend
    workspace_path: ~/code/web-frontend
    role_hint: frontend
    default_reviewers: ["carol"]

  INFRA:
    github_owner: company
    github_repo: k8s-config
    workspace_path: ~/code/k8s-config
    role_hint: infrastructure

cross_project_signals:                 # how to detect cross-project tickets
  jira_labels: ["cross-repo", "x-team"]
  jira_custom_field: "Affected Repos"  # if present + non-empty → cross-project
  component_routes:                    # map Jira components to project keys
    "Auth": [AUTH]
    "Web UI": [WEB]
    "Infrastructure": [INFRA]
```

Loaded once at daemon startup; reloaded on SIGHUP. The Pydantic model:

```python
class ProjectMapping(BaseModel):
    default: ProjectRoute | None = None
    projects: dict[str, ProjectRoute]    # keyed by Jira project key
    cross_project_signals: CrossProjectSignals

class ProjectRoute(BaseModel):
    github_owner: str
    github_repo: str
    workspace_path: Path
    description: str | None = None
    default_base_branch: str = "main"
    default_reviewers: list[str] = []
    role_hint: str | None = None         # "backend" | "frontend" | "infrastructure" | ...

class CrossProjectSignals(BaseModel):
    jira_labels: list[str] = []
    jira_custom_field: str | None = None
    component_routes: dict[str, list[str]] = {}
```

### Project routing per ticket

`ProjectRouter.route(jira_ticket: JiraTicket) -> TicketRouting`:

```python
@dataclass(frozen=True)
class TicketRouting:
    primary_project: ProjectRoute            # the project this ticket lives in
    is_cross_project: bool
    affected_projects: list[AffectedProject] # populated when is_cross_project
    current_workspace_matches: bool          # does the local daemon's workspace match primary?

@dataclass(frozen=True)
class AffectedProject:
    project_key: str
    route: ProjectRoute
    role_hint: str | None                    # "backend" / "frontend" / etc.
```

`route()` logic:

1. **Primary project**: derive from `jira_ticket.project_key`; look up in `projects`; fall back to `default`. If neither exists, raise `UnknownProjectError`.
2. **Cross-project detection**: check (in order):
   - `cross_project_signals.jira_custom_field` present and non-empty on the ticket → cross-project; affected projects are the values
   - any of `cross_project_signals.jira_labels` present on the ticket → cross-project; affected projects derived from components
   - more than one Jira component AND each maps to a different project_key via `component_routes` → cross-project; affected projects are those mapped projects
3. **Affected projects ordering**: alphabetical by project_key, except `primary_project` is first.
4. **Workspace match**: compare `primary_project.workspace_path.resolve()` to the daemon's configured `workspace_root`. Equality (path-normalized) → match.

### Multi-project: ticket-list view

`list_my_tickets()` returns tickets across ALL Jira projects assigned to the current user. Each ticket is enriched with `TicketRouting`:

```python
class TicketWithRouting(BaseModel):
    jira_ticket: JiraTicket
    routing: TicketRouting

async def list_my_tickets(
    statuses: list[JiraStatus] | None = None,
    limit: int = 50,
) -> list[TicketWithRouting]: ...
```

The Dashboard renders this as a grouped table (by `primary_project.github_repo`). Tickets where `routing.current_workspace_matches is False` are visually flagged with the target workspace path.

The CLI `ai-coding tickets list` command renders the same data as a text table.

### Reaction routing (multi-project)

The daemon reacts to Jira webhooks for tickets assigned to the current user, regardless of project. But the daemon only acts on a ticket if its workspace matches:

```python
async def react(event: JiraStateChangeEvent) -> None:
    ticket = await jira.read(event.jira_key)
    routing = router.route(ticket)
    if not routing.current_workspace_matches:
        # Notify the developer, don't act.
        await dashboard_notifier.notify_workspace_mismatch(
            jira_key=event.jira_key,
            expected_workspace=routing.primary_project.workspace_path,
        )
        return
    await orchestrator.react(event, routing)
```

Workspace mismatch handling:

- Operation log NOT written (the daemon did nothing meaningful).
- A Dashboard notification appears: "Ticket X needs work in workspace Y — switch your VS Code window and re-react via CLI."
- The CLI command `ai-coding pipeline status <KEY>` lets the developer manually re-trigger reaction in the correct workspace.

### Cross-project: design phase

`CrossProjectDesignHandler` (ADR-0004) uses `routing.affected_projects` to:

1. Determine the primary repo for the design Issue (it's `affected_projects[0]`, which equals `primary_project`).
2. Gather context from each affected repo (read top files via `find_relevant_modules` across multiple workspaces).
3. Compose a Contract section with concrete OpenAPI / Protobuf / GraphQL.
4. Set frontmatter `affected_projects` from `routing.affected_projects` and `implementation_order` from `role_hint` ordering (backend → frontend → infrastructure by default).

Reading files from multiple workspaces requires each affected workspace to be locally cloned. If a workspace is missing, the handler raises `WorkspaceNotFoundError` (FatalError; ticket escalates with a comment listing missing workspaces).

### Cross-project: sub-ticket fan-out

When the cross-project Epic transitions `DESIGN_APPROVED → IN_DEVELOPMENT`, `PipelineOrchestrator._fan_out_sub_tickets(parent_ticket, routing)`:

For each `AffectedProject` in `routing.affected_projects`:

```python
sub_ticket = await jira.create_sub_task(
    parent_key=parent_ticket.key,
    project_key=affected.project_key,
    summary=f"[{affected.role_hint}] {parent_ticket.summary}",
    description=cross_project_sub_ticket_description(
        parent_url=parent_ticket.url,
        design_issue_url=parent_ticket.custom_fields["Design Issue"],
        contract_anchor=f"{design_issue_url}#contract",
    ),
    labels=["cross-project-sub", f"role:{affected.role_hint}"],
    custom_fields={
        "Parent Epic": parent_ticket.key,
        "Affected Repos": [parent_ticket.key],
    },
    initial_status="IN_DEVELOPMENT",     # design is already approved at parent level
    assignee=affected.route.default_reviewers[0] if affected.route.default_reviewers else None,
)
```

The orchestrator then transitions the parent Epic to `IN_DEVELOPMENT` with the `parent-cross-project` label, and the parent's handler becomes a passive watcher that polls sub-ticket statuses (or subscribes to Jira automation rule events) until all sub-tickets reach DONE.

### Cross-project: each sub-ticket is independent

Each generated sub-ticket runs its own pipeline (Stage 2 → 3 → 4 → 5 → 6) in its assignee's workspace. The contract section in the parent Epic's design Issue is the shared truth; sub-tickets reference it by URL.

The orchestrator on each developer's daemon reacts to sub-tickets assigned to that developer's user. Sub-tickets assigned to a different developer are visible in `list_my_tickets` only if the current user is also an assignee, watcher, or reporter (Jira's standard query semantics).

### Cross-project: contract drift detection

Stage 3 (Self-Review) on a sub-ticket's code includes a contract conformance check:

1. Read the parent Epic's design Issue body.
2. Extract the contract section.
3. Compare the sub-ticket's diff to the contract (schema match, error code coverage).
4. Drift detected → log a Sev-2 finding; the developer must either fix the code or revise the parent Epic's contract section.

The check uses domain-specific tools (openapi-validator, protoc, graphql-inspector) selected by the contract's `type` field.

### Cross-project: deploy ordering

`DeployStageHandler` for sub-tickets respects `implementation_order` from the parent Epic's frontmatter. Concretely:

- Sub-ticket whose role appears earlier in `implementation_order` is allowed to deploy first.
- A sub-ticket whose role is later than the latest-deployed predecessor blocks until the predecessor is deployed.
- Block is enforced by a Jira automation rule that holds `TESTING → DEPLOYING` transition; the agent does not own this rule.

In practice: backend deploys → frontend deploys. The agent's role is to wait, not to coordinate.

### CLI commands

```
ai-coding projects list
    # show configured projects + current workspace match

ai-coding tickets list [--project KEY] [--status STATUS]
    # cross-project ticket list with routing info

ai-coding routing show <JIRA_KEY>
    # diagnose: what project does this ticket map to, is it cross-project,
    # which workspace, does the current workspace match?
```

## Consequences

- One configuration file (`project_mapping.yaml`) is the single source of routing truth per developer.
- Multi-project tickets are listed in one view, with explicit workspace-match flags so developers know which VS Code window to switch to.
- Cross-project tickets are detected by explicit signals (custom field / labels / multi-component), not by guessing — false positives are rare.
- Sub-ticket fan-out is orchestrator-owned, not handler-owned, so a design Issue review doesn't accidentally trigger work in other repos.
- Each sub-ticket runs independently; cross-project coordination is mediated by Jira (parent Epic completion rule + deploy-order automation), not by the agent runtime.
- Contract drift between sub-ticket implementations is caught at self-review time, not at integration time.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether project_mapping.yaml should also live in a team-shared location (so teammates inherit defaults but override workspace paths) | Phase 1 implementation |
| Q2 | How a ticket's `Affected Repos` custom field is populated initially — manually by triage, or by an agent suggestion | Triage workflow doc |
| Q3 | Cross-project deploy coordination when one repo's deploy fails mid-flight | Operational deployment doc |
| Q4 | Whether cross-project Epic completion can be enforced inside the agent (without Jira automation) | ADR for resilience features |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline Business Model
- ADR-0004 Stage 1 Design Flow
- ADR-0028 Jira Workflow Specification
- ADR-0029 Jira Reaction Mechanism

## Reviewers

- [ ] Taven
