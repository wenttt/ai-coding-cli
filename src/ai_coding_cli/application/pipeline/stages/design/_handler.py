"""DesignStageHandler dispatcher. See ADR-0004."""

from __future__ import annotations

from ..._context import StageContext, StageResult
from ._brownfield import BrownfieldDesignHandler


class DesignStageHandler:
    """Stage 1 dispatcher per ADR-0004.

    In Lite this routes brownfield only. Greenfield + cross-project sub-handlers
    are stubbed: when the dispatcher hits those branches, it falls back to the
    brownfield path so the ticket still moves forward (with a note in the
    operation log).
    """

    stage_name = "design"
    entry_status = "DESIGN_DRAFTING"
    exit_status_on_success = "DESIGN_REVIEW"
    exit_status_on_failure = "DESIGN_DRAFTING"
    max_retries = 3

    def __init__(self) -> None:
        self._brownfield = BrownfieldDesignHandler()

    async def run(self, ctx: StageContext) -> StageResult:
        if ctx.is_cross_project:
            # Cross-project routing lands in a later phase. For now, fall through
            # to brownfield so the ticket still gets a Design Issue.
            return await self._brownfield.run(ctx)
        if ctx.mode == "greenfield":
            # Greenfield handler lands in a later phase. Fall through.
            return await self._brownfield.run(ctx)
        return await self._brownfield.run(ctx)
