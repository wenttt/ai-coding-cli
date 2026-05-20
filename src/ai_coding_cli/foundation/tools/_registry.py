"""ToolRegistry: holds registered Tools, dispatches calls. See ADR-0013."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from ..errors import ToolNotFoundError
from ._context import ToolContext
from ._result import ToolResult, ToolResultStatus
from ._side_effects import SideEffectRecorder
from ._tool import Tool

_DEFAULT_TIMEOUT_SECONDS = 60.0


class ToolRegistry:
    """In-process tool registry. One instance per daemon (typically the
    global registry); tests use throwaway instances.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool_obj: Tool) -> None:
        if tool_obj.name in self._tools:
            existing = self._tools[tool_obj.name]
            if existing is tool_obj:
                return
            raise ValueError(
                f"Tool {tool_obj.name!r} is already registered. "
                f"Existing: {type(existing).__name__}; new: {type(tool_obj).__name__}."
            )
        self._tools[tool_obj.name] = tool_obj

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                f"No tool named {name!r} is registered.",
                tool_name=name,
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas_for_llm(
        self,
        *,
        allow_only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate OpenAI tool-calling schemas, filtered by visibility + caller flags."""
        exclude_set = set(exclude or [])
        allow_set = set(allow_only) if allow_only is not None else None
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            if not t.visible_to_agent:
                continue
            if t.name in exclude_set:
                continue
            if allow_set is not None and t.name not in allow_set:
                continue
            out.append(t.to_openai_schema())
        return out

    async def call(
        self,
        name: str,
        arguments: dict[str, Any] | BaseModel,
        ctx: ToolContext,
    ) -> ToolResult:
        """Validate args, dispatch, return a ToolResult.

        Tool errors are returned as `status=ERROR`, not raised — the Agent Core
        feeds them back to the LLM. Argument validation errors are the same.
        Timeouts return `status=TIMEOUT`. The only exception that propagates
        is ToolNotFoundError from get().
        """
        tool_obj = self.get(name)
        invocation_id = uuid4().hex

        # Validate arguments
        try:
            if isinstance(arguments, BaseModel):
                args_model = arguments
            else:
                args_model = tool_obj.input_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.error(
                tool_name=name,
                invocation_id=invocation_id,
                message=f"Invalid arguments: {exc}",
            )

        recorder = SideEffectRecorder(default_class=tool_obj.side_effects)
        ctx_with_recorder = _attach_recorder(ctx, recorder)

        timeout = tool_obj.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
        started = time.monotonic()
        try:
            raw_value = await asyncio.wait_for(
                tool_obj.call(args_model, ctx_with_recorder),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult.timeout(
                tool_name=name,
                invocation_id=invocation_id,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - tool errors stay in ToolResult
            return ToolResult.error(
                tool_name=name,
                invocation_id=invocation_id,
                message=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        # Serialize output for the LLM
        if tool_obj.output_model is not None and isinstance(raw_value, dict):
            content = tool_obj.output_model.model_validate(raw_value).model_dump_json()
        elif tool_obj.output_model is not None and isinstance(raw_value, BaseModel):
            content = raw_value.model_dump_json()
        elif isinstance(raw_value, str):
            content = raw_value
        else:
            try:
                content = json.dumps(raw_value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                content = repr(raw_value)

        return ToolResult.success(
            tool_name=name,
            invocation_id=invocation_id,
            content=content,
            raw_value=raw_value,
            duration_seconds=time.monotonic() - started,
            side_effects=recorder.records,
        )


def _attach_recorder(ctx: ToolContext, recorder: SideEffectRecorder) -> ToolContext:
    """Tools that want to record side effects access ctx._recorder. We attach
    it dynamically to keep the ToolContext frozen dataclass strict otherwise.
    """
    object.__setattr__(ctx, "_recorder", recorder)
    return ctx


# ---------------------------------------------------------------------------
# Global registry singleton
# ---------------------------------------------------------------------------

_global_registry: ToolRegistry | None = None


def global_registry() -> ToolRegistry:
    """Return the process-wide tool registry. Tests can swap via reset_global_registry()."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def reset_global_registry() -> None:
    """Tests only. Replace the global registry with a fresh one."""
    global _global_registry
    _global_registry = ToolRegistry()
