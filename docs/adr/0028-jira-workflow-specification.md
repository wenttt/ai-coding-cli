# ADR-0028: Jira Workflow Specification

## Status

Proposed

## Date

2026-05-19

## Context

Specify the Jira workflow that drives the pipeline. The specification is the project's reference; Jira admins implement it on the company Jira instance.

## Decision

### Statuses

7 statuses. Each status maps to a pipeline phase. Sub-status detail goes to labels and operation logs, not the workflow.

| Status | Category | Meaning |
|---|---|---|
| TODO | To Do | Triage / not yet started |
| DESIGN_DRAFTING | In Progress | Agent is generating the design |
| DESIGN_REVIEW | In Progress | Design ready; awaiting reviewer approval |
| DESIGN_REWORK | In Progress | Reviewer requested changes; agent revising |
| IN_DEVELOPMENT | In Progress | Agent is writing code on a feature branch |
| CODE_REVIEW | In Progress | Code PR open; awaiting reviewer or merge |
| CODE_REWORK | In Progress | Reviewer requested changes; agent revising code |
| TESTING | In Progress | Tests being written / executed; retry-fix loop |
| DEPLOYING | In Progress | Deploy triggered; awaiting confirmation |
| DONE | Done | Pipeline complete; docs updated; ticket closed |

(Total reads as 10 statuses, but DESIGN_REWORK and CODE_REWORK are short-lived re-entry states. The pipeline progression has 7 forward statuses.)

### Transitions

Each transition is named so Jira renders meaningful buttons in the UI.

| From | To | Transition name | Trigger |
|---|---|---|---|
| TODO | DESIGN_DRAFTING | "Start design" | Agent (when developer invokes `ai-coding chat "start KAN-4"`) |
| DESIGN_DRAFTING | DESIGN_REVIEW | "Submit for review" | Agent (when design generated + Issue opened) |
| DESIGN_REVIEW | DESIGN_APPROVED → IN_DEVELOPMENT | "Approve & proceed" | Reviewer |
| DESIGN_REVIEW | DESIGN_REWORK | "Request changes" | Reviewer |
| DESIGN_REWORK | DESIGN_DRAFTING | "Resume design" | Agent (when revising) |
| IN_DEVELOPMENT | CODE_REVIEW | "Submit code for review" | Agent (when PR opened) |
| CODE_REVIEW | TESTING | "Approve code" | Reviewer or auto-on-merge |
| CODE_REVIEW | CODE_REWORK | "Request code changes" | Reviewer |
| CODE_REWORK | CODE_REVIEW | "Push revised code" | Agent (after addressing comments) |
| TESTING | DEPLOYING | "Tests passed, deploy" | Agent (when tests green) |
| TESTING | CODE_REWORK | "Tests revealed code issue" | Agent (when retry-fix loop hits something beyond fix budget) |
| DEPLOYING | DONE | "Deploy confirmed" | Agent (after smoke + doc update) |

(DESIGN_APPROVED is a transient pseudo-status; the "Approve & proceed" transition moves directly from DESIGN_REVIEW to IN_DEVELOPMENT to avoid an extra click.)

### Transition permissions

Use Jira's built-in transition permission model. Recommended grants:

| Transition | Allowed by |
|---|---|
| Start design | Assignee, reporter |
| Submit for review | Agent (service account), assignee |
| Approve & proceed | Designated reviewers (typically tech leads + product) |
| Request changes | Designated reviewers |
| Resume design | Agent (service account) |
| Submit code for review | Agent (service account), assignee |
| Approve code | Designated reviewers; OR triggered automatically by GitHub PR merge via webhook |
| Request code changes | Designated reviewers |
| Push revised code | Agent (service account) |
| Tests passed, deploy | Agent (service account), DevOps role |
| Tests revealed code issue | Agent (service account) |
| Deploy confirmed | Agent (service account), DevOps role |

The agent acts as a Jira service account so its transitions are auditable distinct from human transitions.

### Required custom fields

Add these fields to the ticket screens:

| Field | Type | Purpose |
|---|---|---|
| `Agent Operation Log Path` | URL | Link to `docs/operations/{KEY}/` for the current ticket |
| `Design Issue` | URL | Link to the GitHub Issue holding the design markdown |
| `Implementation PR` | URL | Link to the GitHub PR with code |
| `Retry Count` | Number | Current retry count for the active stage (max 3, then escalate label) |
| `Affected Repos` | Multi-select / Labels | For cross-project tickets, the repos this ticket touches |
| `Mode` | Single-select | `brownfield` or `greenfield` |

### Labels (sub-status)

Labels carry fine-grained state without polluting the workflow:

- `escalated` — 3-strike retry budget exhausted; halts agent action; humans must resolve
- `cross-project` — touches more than one repo
- `agent-paused` — developer manually paused agent reactions on this ticket
- `requires-graph-context` — agent should query Neo4j module graph before next action

### Cross-project tickets

A cross-project Epic in the primary project carries `cross-project` label. Sub-tasks live in their respective projects, each running its own copy of the workflow. The Epic's status reaches DONE only when all sub-tasks reach DONE; this rule is enforced by a Jira automation rule (not by the agent).

### Reference Jira admin setup

Bundle these artifacts in the project repo at `docs/jira/`:

- `workflow.json` — exportable Jira workflow definition (Cloud + Server format)
- `screens.json` — screen configurations including the custom fields above
- `permissions.md` — guidance on which roles to assign to which transitions
- `setup-checklist.md` — step-by-step for a Jira admin

## Consequences

- Pipeline state is visible in the Jira board / Kanban without any custom UI. Business users, PMs, QA see it natively.
- Approval gates use Jira's transition permission model. We don't reimplement role-based auth.
- Audit trail is the Jira changelog (automatic).
- The agent acts via a Jira service account, distinguishable from human actions in the audit log.
- Each adopting team needs a Jira admin to apply the reference workflow. Setup cost is one-time per project.
- The 7-status forward path is opinionated; teams that already have a different workflow need to migrate or run two workflows in parallel during transition.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Jira automation rule details for cross-project Epic completion | Implementation-phase decision |
| Q2 | Service account naming + permission scope (e.g. `agent-ai-coding`) | ADR for production deployment |
| Q3 | Migration path for teams with existing Jira workflows | Separate migration guide doc |

## References

- ADR-0001 System Overview
- ADR-0003 Pipeline business model (covers the state machine in detail; this ADR is the Jira-side specification)
- ADR-0029 Jira reaction mechanism (how the agent observes + reacts to status changes)
- Atlassian Jira workflow documentation (for admins implementing this spec)

## Reviewers

- [ ] Taven
