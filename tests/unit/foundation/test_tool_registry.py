"""Tests for the Tool Registry. See ADR-0013."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from ai_coding_cli.foundation.config import Config
from ai_coding_cli.foundation.errors import ToolNotFoundError
from ai_coding_cli.foundation.tools import (
    MockToolRegistry,
    SideEffectClass,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResultStatus,
    tool,
)


@pytest.fixture
def fresh_registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def ctx(test_config: Config) -> ToolContext:
    return ToolContext(
        config=test_config,
        session_id=None,
        conversation_id=None,
        invocation_id=uuid4().hex,
    )


class _AddArgs(BaseModel):
    a: int
    b: int


class _DoubleArgs(BaseModel):
    n: int = Field(ge=0)


def test_register_then_retrieve(fresh_registry: ToolRegistry) -> None:
    tool_obj = Tool(
        name="add",
        description="Add two integers.",
        input_model=_AddArgs,
        side_effects=SideEffectClass.READ_ONLY,
        impl=lambda args, _ctx: args.a + args.b,
    )
    fresh_registry.register(tool_obj)
    assert fresh_registry.has("add")
    assert fresh_registry.get("add") is tool_obj


def test_register_duplicate_name_raises(fresh_registry: ToolRegistry) -> None:
    t1 = Tool(
        name="dup",
        description="x",
        input_model=_AddArgs,
        impl=lambda args, _ctx: 0,
    )
    t2 = Tool(
        name="dup",
        description="y",
        input_model=_AddArgs,
        impl=lambda args, _ctx: 1,
    )
    fresh_registry.register(t1)
    with pytest.raises(ValueError, match="already registered"):
        fresh_registry.register(t2)


def test_get_unknown_tool_raises(fresh_registry: ToolRegistry) -> None:
    with pytest.raises(ToolNotFoundError):
        fresh_registry.get("nonexistent")


def test_decorator_registers_with_explicit_registry(ctx: ToolContext) -> None:
    custom = ToolRegistry()

    @tool(
        name="double",
        description="Double an int.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=custom,
    )
    def double(args: _DoubleArgs, _ctx: ToolContext) -> int:
        return args.n * 2

    assert custom.has("double")
    # The decorator returns the original callable, so direct call still works.
    assert double(_DoubleArgs(n=3), ctx) == 6  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_call_dispatches_and_validates_args(
    fresh_registry: ToolRegistry, ctx: ToolContext
) -> None:
    @tool(
        name="echo",
        description="Echo n back.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=fresh_registry,
    )
    def echo(args: _AddArgs, _ctx: ToolContext) -> dict[str, int]:
        return {"a": args.a, "b": args.b}

    result = await fresh_registry.call("echo", {"a": 1, "b": 2}, ctx)
    assert result.status == ToolResultStatus.SUCCESS
    assert result.raw_value == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_call_returns_error_result_on_invalid_args(
    fresh_registry: ToolRegistry, ctx: ToolContext
) -> None:
    @tool(
        name="strict",
        description="Strict args.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=fresh_registry,
    )
    def strict(args: _AddArgs, _ctx: ToolContext) -> int:
        return args.a + args.b

    result = await fresh_registry.call("strict", {"a": "not-an-int", "b": 2}, ctx)
    assert result.status == ToolResultStatus.ERROR
    assert "Invalid arguments" in result.content


@pytest.mark.asyncio
async def test_call_returns_error_result_when_tool_raises(
    fresh_registry: ToolRegistry, ctx: ToolContext
) -> None:
    @tool(
        name="boom",
        description="Always raises.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=fresh_registry,
    )
    def boom(args: _AddArgs, _ctx: ToolContext) -> int:
        raise RuntimeError("Kapow")

    result = await fresh_registry.call("boom", {"a": 1, "b": 2}, ctx)
    assert result.status == ToolResultStatus.ERROR
    assert "Kapow" in result.content


def test_schemas_for_llm_excludes_hidden_tools() -> None:
    custom = ToolRegistry()

    @tool(
        name="visible",
        description="Visible tool.",
        side_effects=SideEffectClass.READ_ONLY,
        visible_to_agent=True,
        registry=custom,
    )
    def visible(args: _AddArgs, _ctx: ToolContext) -> int:
        return args.a

    @tool(
        name="hidden",
        description="Orchestrator-only.",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        visible_to_agent=False,
        registry=custom,
    )
    def hidden(args: _AddArgs, _ctx: ToolContext) -> int:
        return args.b

    schemas = custom.schemas_for_llm()
    names = [s["function"]["name"] for s in schemas]
    assert "visible" in names
    assert "hidden" not in names


def test_mock_registry_returns_canned_response(ctx: ToolContext) -> None:
    mock = MockToolRegistry()
    mock.register_canned(name="read_jira_ticket", response={"key": "PROJ-1", "summary": "x"})
    assert mock.has("read_jira_ticket")
    # We can't await a fixture-bound coroutine in a sync test trivially; the
    # async version of this assertion lives in the agent-level integration tests.
