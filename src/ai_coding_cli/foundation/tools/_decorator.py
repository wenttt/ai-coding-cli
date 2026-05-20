"""@tool decorator. See ADR-0013.

Usage:

    from pydantic import BaseModel, Field

    class ReadRepoFileArgs(BaseModel):
        path: str = Field(description="Path relative to workspace_root")
        max_bytes: int = Field(default=200_000, ge=1, le=2_000_000)

    @tool(
        name="read_repo_file",
        description="Read a file from the workspace.",
        side_effects=SideEffectClass.READ_ONLY,
    )
    async def read_repo_file(args: ReadRepoFileArgs, ctx: ToolContext) -> str:
        ...
"""

from __future__ import annotations

import inspect
import sys
import typing
from typing import Any, Callable

from pydantic import BaseModel

from ._registry import ToolRegistry, global_registry
from ._side_effects import SideEffectClass
from ._tool import Tool, ToolImpl


def _resolve_first_param_annotation(func: Callable[..., Any]) -> Any:
    """Resolve the first positional parameter's annotation to a real class.

    Modules that use `from __future__ import annotations` (PEP 563) store
    annotations as strings. `typing.get_type_hints` re-evaluates them in the
    function's defining module's globals so we get the actual class back.
    """
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception:
        # If a hint references a name that's not yet importable, fall back to
        # raw inspection. The caller will surface a clearer TypeError below.
        hints = {}
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    if not params:
        return None
    first = params[0]
    if first.name in hints:
        return hints[first.name]
    annotation = first.annotation
    if isinstance(annotation, str):
        # Last-resort: eval inside the func's module globals.
        module = sys.modules.get(func.__module__)
        globalns = getattr(module, "__dict__", {}) if module else {}
        try:
            return eval(annotation, globalns)  # noqa: S307
        except Exception:
            return annotation
    return annotation


def tool(
    *,
    name: str,
    description: str,
    side_effects: SideEffectClass = SideEffectClass.READ_ONLY,
    requires_confirmation: bool | None = None,
    timeout_seconds: float | None = None,
    visible_to_agent: bool = True,
    output_model: type[BaseModel] | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[[ToolImpl], ToolImpl]:
    """Register `func` as a Tool.

    The function's first positional argument MUST be annotated with a
    Pydantic BaseModel subclass; that becomes the tool's `input_model`.
    The second argument is typed as `ToolContext` by convention.

    Returns the original function unchanged, so it can still be called
    directly in tests.
    """

    def decorator(func: ToolImpl) -> ToolImpl:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        if len(params) < 1:
            raise TypeError(
                f"@tool function {func.__name__!r} must accept at least one argument "
                "(the Pydantic input model)."
            )

        # First positional param: input model class from annotation.
        first = params[0]
        input_model = _resolve_first_param_annotation(func)
        if not (isinstance(input_model, type) and issubclass(input_model, BaseModel)):
            raise TypeError(
                f"@tool function {func.__name__!r}: first parameter "
                f"{first.name!r} must be annotated with a Pydantic BaseModel subclass "
                f"(got {input_model!r})."
            )

        Tool(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            side_effects=side_effects,
            requires_confirmation=requires_confirmation,
            timeout_seconds=timeout_seconds,
            visible_to_agent=visible_to_agent,
            impl=func,
        )._self_register(registry)

        return func

    return decorator


# Attach a small helper to Tool that the decorator uses to register itself.
# Done here to avoid a circular import in _tool.py.
def _self_register(self: Tool, registry: ToolRegistry | None) -> None:
    target = registry or global_registry()
    target.register(self)


Tool._self_register = _self_register  # type: ignore[attr-defined]
