"""Tool: the protocol every tool implements. See ADR-0013."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from ._context import ToolContext
from ._side_effects import SideEffectClass

# A tool implementation function: takes (args_model, ctx) and returns a
# domain value or coroutine of one. Sync funcs are supported and adapted.
ToolImpl = Callable[..., Any] | Callable[..., Awaitable[Any]]


class Tool:
    """A registered tool.

    Concrete tools are created via the `@tool(...)` decorator, which builds
    a Tool instance and registers it in the global ToolRegistry.

    Attributes:
        name: stable, unique within a ToolRegistry
        description: passed to the LLM as the tool's documentation
        input_model: Pydantic BaseModel subclass; arguments are validated
            before the function runs
        output_model: optional Pydantic model; when set, output is validated
            + serialized via model_dump_json. When None, output is
            json.dumps'd as best-effort.
        side_effects: classifies what the tool does for the Guardrail layer
        requires_confirmation: per-tool override for Action Guardrail; default
            True for EXTERNAL_WRITE + DESTRUCTIVE, False otherwise
        timeout_seconds: per-tool override; falls back to AgentConfig.tool_call_timeout_seconds
        visible_to_agent: when False, the tool is registered but hidden from
            the schema list passed to the LLM (orchestrator-only tools)
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel] | None = None,
        side_effects: SideEffectClass = SideEffectClass.READ_ONLY,
        requires_confirmation: bool | None = None,
        timeout_seconds: float | None = None,
        visible_to_agent: bool = True,
        impl: ToolImpl,
    ) -> None:
        self.name = name
        self.description = description
        self.input_model = input_model
        self.output_model = output_model
        self.side_effects = side_effects
        # Default requires_confirmation policy (per ADR-0013):
        #   EXTERNAL_WRITE + DESTRUCTIVE -> True; others -> False.
        if requires_confirmation is None:
            requires_confirmation = side_effects in (
                SideEffectClass.EXTERNAL_WRITE,
                SideEffectClass.DESTRUCTIVE,
            )
        self.requires_confirmation = requires_confirmation
        self.timeout_seconds = timeout_seconds
        self.visible_to_agent = visible_to_agent
        self._impl = impl

    async def call(self, args: BaseModel, ctx: ToolContext) -> Any:
        """Invoke the underlying implementation. Adapts sync impls to async."""
        result = self._impl(args, ctx)
        if hasattr(result, "__await__"):
            return await result
        return result

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate the OpenAI function-calling schema entry for this tool."""
        params_schema = self.input_model.model_json_schema()
        # Inline $defs so OpenAI consumes a flat schema.
        params_schema = _inline_refs(params_schema)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params_schema,
            },
        }


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $ref / $defs inline; strip Pydantic-specific keys OpenAI ignores."""
    defs = schema.pop("$defs", {})

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and len(node) == 1:
                # "$ref": "#/$defs/Name" -> inline the referenced object
                ref = node["$ref"]
                key = ref.rsplit("/", 1)[-1]
                return _walk(defs.get(key, {}))
            return {k: _walk(v) for k, v in node.items() if not k.startswith("$ref")}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        return node

    return _walk(schema)
