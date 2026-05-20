"""MockToolRegistry for agent-level tests. See ADR-0013 + ADR-0018."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ._context import ToolContext
from ._result import ToolResult


class MockToolRegistry:
    """A ToolRegistry stand-in that returns pre-programmed responses.

    Usage in tests:

        mock = MockToolRegistry()
        mock.register_canned(
            name="read_jira_ticket",
            response={"key": "PROJ-1", "summary": "...", ...},
        )
        result = await mock.call("read_jira_ticket", {"jira_key": "PROJ-1"}, ctx)
        assert result.is_success
    """

    def __init__(self) -> None:
        self._canned: dict[str, Any] = {}
        self._schemas: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def register_canned(self, *, name: str, response: Any, schema: dict[str, Any] | None = None) -> None:
        self._canned[name] = response
        if schema is not None:
            self._schemas.append(schema)
        else:
            self._schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Mock tool {name!r}.",
                    "parameters": {"type": "object", "properties": {}},
                },
            })

    def has(self, name: str) -> bool:
        return name in self._canned

    def schemas_for_llm(
        self,
        *,
        allow_only: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        exclude_set = set(exclude or [])
        allow_set = set(allow_only) if allow_only is not None else None
        out = []
        for s in self._schemas:
            n = s["function"]["name"]
            if n in exclude_set:
                continue
            if allow_set is not None and n not in allow_set:
                continue
            out.append(s)
        return out

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,  # noqa: ARG002 - kept for interface parity
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name not in self._canned:
            return ToolResult.error(
                tool_name=name,
                invocation_id=uuid4().hex,
                message=f"Mock has no canned response for {name!r}.",
            )
        response = self._canned[name]
        return ToolResult.success(
            tool_name=name,
            invocation_id=uuid4().hex,
            content=str(response) if not isinstance(response, str) else response,
            raw_value=response,
        )
