"""StageHandler Protocol. See ADR-0003."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._context import StageContext, StageResult


@runtime_checkable
class StageHandler(Protocol):
    """One pipeline stage. ADR-0003 §Stage handler interface.

    Handlers are pure: they take a StageContext, return a StageResult. They
    do NOT touch Jira state directly; the orchestrator does that based on
    `result.outcome`.
    """

    stage_name: str
    entry_status: str
    exit_status_on_success: str
    exit_status_on_failure: str
    max_retries: int

    async def run(self, ctx: StageContext) -> StageResult: ...
