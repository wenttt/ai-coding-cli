"""Pipeline state machine. See ADR-0003 + ADR-0028."""

from __future__ import annotations

from ._handler import StageHandler

# Canonical Jira status names. Match ADR-0028 §Statuses.
PIPELINE_STATUSES = frozenset(
    {
        "TODO",
        "DESIGN_DRAFTING",
        "DESIGN_REVIEW",
        "DESIGN_REWORK",
        "IN_DEVELOPMENT",
        "CODE_REVIEW",
        "CODE_REWORK",
        "TESTING",
        "DEPLOYING",
        "DONE",
    }
)


class PipelineStateMachine:
    """Maps each agent-actionable status to a registered StageHandler.

    Statuses without a handler are passive (waiting for human action) and
    cause the orchestrator's react() to no-op.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, StageHandler] = {}

    def register(self, handler: StageHandler) -> None:
        if handler.entry_status not in PIPELINE_STATUSES:
            raise ValueError(
                f"Handler {handler.stage_name!r} declares unknown entry_status "
                f"{handler.entry_status!r}."
            )
        if handler.entry_status in self._handlers:
            raise ValueError(
                f"Status {handler.entry_status!r} already has handler "
                f"{self._handlers[handler.entry_status].stage_name!r}."
            )
        self._handlers[handler.entry_status] = handler

    def handler_for(self, status: str) -> StageHandler | None:
        return self._handlers.get(status)

    def all_handled_statuses(self) -> list[str]:
        return list(self._handlers.keys())
