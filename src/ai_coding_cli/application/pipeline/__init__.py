"""Pipeline orchestration. See ADR-0003.

Public exports:
    - PipelineOrchestrator
    - StageHandler (Protocol)
    - StageContext, StageResult
    - JiraStateChangeEvent
    - PIPELINE_STATE_MACHINE: status -> handler mapping
"""

from __future__ import annotations

from ._context import StageContext, StageResult
from ._event import JiraStateChangeEvent
from ._handler import StageHandler
from ._orchestrator import PipelineOrchestrator
from ._state_machine import (
    PIPELINE_STATUSES,
    PipelineStateMachine,
)

__all__ = [
    "PipelineOrchestrator",
    "StageHandler",
    "StageContext",
    "StageResult",
    "JiraStateChangeEvent",
    "PipelineStateMachine",
    "PIPELINE_STATUSES",
]
