# ADR-0003: Pipeline Business Model

## Status

Accepted

## Date

2026-05-19

## Context

Define the AI Coding Workflow pipeline as a state machine over Jira ticket status. Specify stage handler interface, orchestrator behavior, retry / escalation, branching for brownfield / greenfield / cross-project tickets.

## Decision

### State machine

The Jira ticket status (ADR-0028) is the canonical state. A `PipelineStateMachine` maps each agent-actionable status to a stage handler. Statuses that wait for human action have no handler.

| Jira status | Handler | Notes |
|---|---|---|
| TODO | — | Idle; transitions when developer invokes the agent. |
| DESIGN_DRAFTING | `DesignStageHandler` | Generates design; transitions to DESIGN_REVIEW on completion. |
| DESIGN_REVIEW | — | Waits for reviewer. |
| DESIGN_REWORK | `DesignReworkHandler` | Reads reviewer comments; updates design; transitions back to DESIGN_DRAFTING. |
| IN_DEVELOPMENT | `ImplementationStageHandler` | Writes code; opens PR; transitions to CODE_REVIEW. |
| CODE_REVIEW | `CodeReviewWaitHandler` | Passive: subscribes to GitHub PR merge / reviewer status change. |
| CODE_REWORK | `CodeReworkHandler` | Addresses review comments; pushes; transitions back to CODE_REVIEW. |
| TESTING | `TestStageHandler` | Writes + runs tests; retry-fix loop; transitions to DEPLOYING on green. |
| DEPLOYING | `DeployStageHandler` | Triggers deploy; transitions to DONE on confirmation. |
| DONE | `DocUpdateHandler` | One-shot: updates docs + posts final comment; idempotent. |

### Stage handler interface

```python
class StageHandler(Protocol):
    """Each handler implements one stage of the pipeline."""

    stage_name: str                           # "design" | "design-rework" | "implement" | ...
    entry_status: JiraStatus
    exit_status_on_success: JiraStatus
    exit_status_on_failure: JiraStatus        # status to set when handler fails before escalation
    max_retries: int = 3                      # per-stage retry budget

    async def run(self, ctx: StageContext) -> StageResult: ...


@dataclass(frozen=True)
class StageContext:
    jira_key: str
    jira_ticket: JiraTicket                   # already fetched
    prior_operation_logs: list[OperationLog]  # all stages' history for this ticket
    retry_count: int                          # how many times this stage has been run for this ticket
    session: Session                          # cross-stage memory + conversation
    agent: Agent                              # ReAct agent (LLM + tools)
    workspace_root: Path
    mode: Literal["brownfield", "greenfield"]
    is_cross_project: bool
    affected_projects: list[ProjectRouting]


@dataclass(frozen=True)
class StageResult:
    outcome: Literal["completed", "failed", "escalated"]
    summary: str
    artifacts: dict[str, str]                 # e.g. {"design_issue_url": "...", "pr_url": "..."}
    operation_log_body: OperationLogBody      # structured body for the operation log
```

### Orchestrator

```python
class PipelineOrchestrator:
    async def react(self, event: JiraStateChangeEvent) -> None:
        handler = self.state_machine.handler_for(event.to_status)
        if handler is None:
            return  # status requires human action; no agent reaction

        ctx = await self._build_context(event, handler)

        # retry budget check
        if ctx.retry_count >= handler.max_retries:
            await self._escalate(ctx, handler, reason="retry budget exhausted")
            return

        try:
            result = await handler.run(ctx)
        except FatalError as exc:
            await self._escalate(ctx, handler, reason=str(exc))
            return
        except RetryableError as exc:
            await self._record_retryable_failure(ctx, handler, exc)
            return

        await self._apply_result(ctx, handler, result)

    async def _apply_result(self, ctx, handler, result: StageResult) -> None:
        # 1. write operation log (always)
        log_path = await self.operation_log_writer.write(ctx, handler, result)
        # 2. transition Jira based on outcome
        if result.outcome == "completed":
            await self.jira.transition(ctx.jira_key, handler.exit_status_on_success)
        elif result.outcome == "failed":
            await self.jira.transition(ctx.jira_key, handler.exit_status_on_failure)
        elif result.outcome == "escalated":
            await self.jira.add_label(ctx.jira_key, "escalated")
        # 3. post Jira comment summarizing what happened
        await self.jira.add_comment(ctx.jira_key, self._comment_for(result, log_path))
```

The orchestrator is the only component that:

- Reads retry budget
- Decides to escalate
- Writes operation logs
- Transitions Jira status
- Posts Jira comments

Stage handlers only produce a `StageResult`. They do not touch Jira state directly.

### Context construction

`_build_context` populates `StageContext` by:

1. Fetching the Jira ticket via `JiraToolClient.read(ticket_key)`.
2. Reading prior operation logs from PostgreSQL (indexed; not by file traversal).
3. Computing `retry_count` from logs filtered by `stage_name`.
4. Calling `analyze_repo_state(workspace_root)` to determine `mode`.
5. Calling `affected_projects_for_ticket(ticket)` to compute cross-project routing.
6. Opening or resuming the `Session` keyed on `(developer_user, jira_key)`.

### Brownfield vs greenfield

`DesignStageHandler` is a dispatcher:

```python
class DesignStageHandler:
    async def run(self, ctx: StageContext) -> StageResult:
        sub = (
            self.greenfield_handler if ctx.mode == "greenfield"
            else self.brownfield_handler
        )
        return await sub.run(ctx)
```

`BrownfieldDesignHandler` and `GreenfieldDesignHandler` are independent classes implementing the `StageHandler` protocol shape (without re-exporting `entry_status` / `exit_status` — they inherit those from the parent).

### Cross-project

A ticket is cross-project when `ctx.is_cross_project == True`. Two effects:

1. `DesignStageHandler` selects the `cross_project.md` template; the design Issue body MUST contain a Contract section (OpenAPI / Protobuf / GraphQL).
2. After approval, the orchestrator generates sub-tickets in the affected projects via `JiraToolClient.create_sub_task(...)`. Each sub-ticket runs its own pipeline independently. The parent (cross-project) Epic transitions to DONE only when all sub-tickets reach DONE (enforced by Jira automation, per ADR-0028).

The orchestrator does NOT serialize stage handlers across the affected projects' workspaces. Each developer's daemon reacts to their own assigned sub-ticket.

### Retry and escalation

Retry count for a `(jira_key, stage_name)` pair:

```python
retry_count = sum(
    1 for log in prior_logs
    if log.stage_name == stage_name and log.outcome != "escalated"
)
```

When a handler raises `RetryableError`, the orchestrator records the failure as an operation log entry with `outcome = "failed"`. The next status change event (or the next manual invocation) re-enters the same handler — retry count is now one higher.

When `retry_count >= handler.max_retries` at entry, the orchestrator skips the handler and directly escalates.

Escalation:

1. Adds `escalated` label to the Jira ticket.
2. Posts a Jira comment summarizing all attempts.
3. Writes an `ESCALATED` operation log.
4. Stops reacting to this ticket until a human removes the `escalated` label (signal: resume from `DESIGN_DRAFTING` on the current status, or re-route manually).

### Passive stages

`CodeReviewWaitHandler` is passive. When the ticket enters CODE_REVIEW, the handler:

1. Subscribes to GitHub PR webhook events for the PR linked to this ticket.
2. On PR merge → transitions ticket to TESTING.
3. On reviewer requesting changes (formal `CHANGES_REQUESTED`) → transitions ticket to CODE_REWORK.

The passive handler does NOT call the LLM. It is a thin event bridge from GitHub to Jira.

Similarly, no handler runs while in DESIGN_REVIEW — but the orchestrator subscribes to Jira-side comment events to remind the developer (via Dashboard notification) if review has been pending more than N hours.

### Pipeline as a library

The orchestrator exposes a single async entry point so the daemon (Jira reaction loop), the CLI (manual invocation), and integration tests all call the same code path:

```python
async def react(event: JiraStateChangeEvent) -> None
async def manual_invoke(jira_key: str, force_stage: str | None = None) -> None  # CLI escape hatch
```

`manual_invoke` synthesizes a synthetic `JiraStateChangeEvent` from the ticket's current status and re-enters `react`.

### Operation log schema (referenced)

Every stage produces an operation log. Schema (Markdown body + YAML frontmatter) is owned by ADR-0005. The orchestrator persists the log via `OperationLogWriter` to both PostgreSQL (indexed metadata) and `docs/operations/{KEY}/{NN}-{stage}-v{N}.md` (git-friendly file).

## Consequences

- The pipeline is one orchestrator + N stage handlers. New stages or branches are added by registering a handler against a status.
- Stage handlers are unit-testable in isolation: inject a `StageContext` with mock agent + mock tools, assert on the returned `StageResult`. No Jira / GitHub roundtrip needed in unit tests.
- The orchestrator centralizes Jira transitions, comments, retry counting, and operation logs. Stage handlers cannot accidentally double-transition or skip logging.
- Cross-project tickets fan out at sub-ticket creation; each developer's daemon owns its assigned sub-tickets independently. No cross-developer coordination needed.
- Passive handlers add complexity (event subscription, webhook routing) but keep the model uniform — every status has either an active or passive handler, none requires special-case orchestrator logic.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Concrete schema of `OperationLogBody` and the `OperationLogWriter` API | ADR-0005 |
| Q2 | How `Session` materializes across handler runs that may be hours apart | ADR-0008 |
| Q3 | Whether DESIGN_REVIEW idle timeout produces a Dashboard nudge or a Jira comment | ADR-0026 |
| Q4 | How `CodeReviewWaitHandler` shares webhook routing infrastructure with the Jira reactor | ADR-0029 + implementation |

## References

- ADR-0001 System Overview (Pipeline Stages section)
- ADR-0028 Jira Workflow Specification (status definitions)
- ADR-0029 Jira Reaction Mechanism (event delivery; this ADR defines reaction logic)
- ADR-0005 Operation log schema (planned)
- ADR-0008 Session + Conversation model (planned)

## Reviewers

- [ ] Taven
