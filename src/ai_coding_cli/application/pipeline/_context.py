"""StageContext + StageResult. See ADR-0003."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ...foundation.agent import Agent
from ...foundation.session import ConversationView, SessionView
from ..operation_log import OperationLogBody, OperationLogSummary


@dataclass(frozen=True)
class StageContext:
    """One stage handler's input. Built by PipelineOrchestrator._build_context."""

    jira_key: str
    jira_ticket: dict[str, Any]      # canonical fields from read_jira_ticket
    prior_logs: list[OperationLogSummary]
    retry_count: int
    session: SessionView
    conversation: ConversationView
    agent: Agent
    workspace_root: Path
    mode: Literal["brownfield", "greenfield"]
    is_cross_project: bool
    delivery_channel: Literal["webhook", "polling", "manual"]


@dataclass(frozen=True)
class StageResult:
    """One stage handler's output. Consumed by PipelineOrchestrator._apply_result."""

    outcome: Literal["completed", "failed", "escalated"]
    summary: str
    artifacts: dict[str, str] = field(default_factory=dict)
    body: OperationLogBody | None = None
    escalation_reason: str | None = None
    next_status_override: str | None = None  # rare: handler chose a non-default next status
