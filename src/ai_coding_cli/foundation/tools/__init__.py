"""Tool Registry. See ADR-0013.

Public exports:
    - Tool, ToolContext, ToolResult, ToolResultStatus
    - SideEffectClass, SideEffectRecord, SideEffectRecorder
    - ToolRegistry
    - tool (decorator for registering a function as a Tool)
    - MockToolRegistry (for tests)
"""

from __future__ import annotations

from ._context import ToolContext
from ._decorator import tool
from ._mock_registry import MockToolRegistry
from ._registry import ToolRegistry, global_registry, reset_global_registry
from ._result import ToolResult, ToolResultStatus
from ._side_effects import SideEffectClass, SideEffectRecord, SideEffectRecorder
from ._tool import Tool

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "ToolResultStatus",
    "SideEffectClass",
    "SideEffectRecord",
    "SideEffectRecorder",
    "ToolRegistry",
    "global_registry",
    "reset_global_registry",
    "tool",
    "MockToolRegistry",
]
