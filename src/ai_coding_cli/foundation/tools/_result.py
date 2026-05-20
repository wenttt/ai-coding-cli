"""ToolResult: the shape returned to the Agent Core from a dispatched tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ._side_effects import SideEffectRecord


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    REFUSED = "refused"


@dataclass(frozen=True)
class ToolResult:
    """A single tool invocation's outcome.

    `content` is the string the LLM sees (typically JSON-encoded output or
    an error message). `raw_value` is the un-serialized Python value for
    in-process consumers; the LLM never sees it.

    Tool errors are wrapped in `ToolResult` (status=ERROR), not raised.
    The Agent Core feeds the result back to the LLM so it can adapt. The
    only exceptions that propagate are MCP bridge startup failures and a
    few other Fatal cases (see ADR-0017).
    """

    tool_name: str
    invocation_id: str
    status: ToolResultStatus
    content: str
    raw_value: Any | None = None
    duration_seconds: float = 0.0
    side_effects_recorded: list[SideEffectRecord] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        *,
        tool_name: str,
        invocation_id: str,
        content: str,
        raw_value: Any = None,
        duration_seconds: float = 0.0,
        side_effects: list[SideEffectRecord] | None = None,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            invocation_id=invocation_id,
            status=ToolResultStatus.SUCCESS,
            content=content,
            raw_value=raw_value,
            duration_seconds=duration_seconds,
            side_effects_recorded=side_effects or [],
        )

    @classmethod
    def error(
        cls,
        *,
        tool_name: str,
        invocation_id: str,
        message: str,
        duration_seconds: float = 0.0,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            invocation_id=invocation_id,
            status=ToolResultStatus.ERROR,
            content=f"[ERROR] {message}",
            duration_seconds=duration_seconds,
        )

    @classmethod
    def timeout(
        cls,
        *,
        tool_name: str,
        invocation_id: str,
        timeout_seconds: float,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            invocation_id=invocation_id,
            status=ToolResultStatus.TIMEOUT,
            content=f"[TIMEOUT] Tool {tool_name!r} exceeded {timeout_seconds}s",
            duration_seconds=timeout_seconds,
        )

    @classmethod
    def refused(
        cls,
        *,
        tool_name: str,
        invocation_id: str,
        reason: str,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            invocation_id=invocation_id,
            status=ToolResultStatus.REFUSED,
            content=f"[REFUSED] {reason}",
        )

    @property
    def is_success(self) -> bool:
        return self.status == ToolResultStatus.SUCCESS
