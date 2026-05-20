"""Agent Core unit tests. See ADR-0009.

Uses MockAdapter + a small in-process ToolRegistry to drive the loop. Each
test scripts the LLM's expected behaviour, runs the loop, and asserts on
the resulting AgentResult + persisted state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest
from pydantic import BaseModel

from ai_coding_cli.foundation.agent import Agent, AgentOutcome
from ai_coding_cli.foundation.compactor import Compactor, CompactorConfig
from ai_coding_cli.foundation.config import build_test_config
from ai_coding_cli.foundation.context import ContextBuilder, RepoFacts
from ai_coding_cli.foundation.errors import LLMRateLimitError
from ai_coding_cli.foundation.llm import MockAdapter
from ai_coding_cli.foundation.llm._adapter import LLMResponse, ToolCall
from ai_coding_cli.foundation.llm._mock import text_response, tool_call_response
from ai_coding_cli.foundation.session import (
    SessionManager,
)
from ai_coding_cli.foundation.storage import BASE, StorageEngine
from ai_coding_cli.foundation.tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
    tool,
)


# ---------------------------------------------------------------------------
# Module-level Pydantic inputs (PEP-563 + locals don't mix; declare globally)
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    text: str


class _WriteArgs(BaseModel):
    path: str


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool(
        name="echo",
        description="Echo input back.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=reg,
    )
    def echo(args: _EchoArgs, _ctx: ToolContext) -> dict[str, str]:
        return {"echoed": args.text}

    @tool(
        name="risky_write",
        description="External write (touches third-party).",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        registry=reg,
    )
    def risky_write(args: _WriteArgs, _ctx: ToolContext) -> dict[str, str]:
        return {"wrote_to": args.path}

    return reg


@pytest.fixture
async def stack(tmp_path: Path, tool_registry: ToolRegistry):
    """Build the full Agent dependency stack against an isolated SQLite db."""
    engine = StorageEngine(tmp_path / "agent_test.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    manager = SessionManager(engine)

    config = build_test_config(WORKSPACE_PATH=str(tmp_path))
    config.agent.max_turns = 5  # fast-fail in tests
    config.llm.rate_limit_retry_max = 2
    config.llm.rate_limit_retry_base_seconds = 0.01

    session = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-T1",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    conversation = await manager.start_conversation(
        session_id=session.id, stage="design"
    )

    adapter = MockAdapter()
    compactor = Compactor(adapter, CompactorConfig(preserve_recent_turns=2))
    builder = ContextBuilder()

    def _make_agent(*, dry_run: bool = False) -> Agent:
        return Agent(
            session=session,
            conversation=conversation,
            llm=adapter,
            tools=tool_registry,
            context_builder=builder,
            compactor=compactor,
            session_manager=manager,
            config=config,
            repo_facts=RepoFacts(languages=["Python"]),
            conventions=None,
            loaded_skills=[],
            operation_log_path=None,
            dry_run=dry_run,
        )

    yield {
        "engine": engine,
        "manager": manager,
        "adapter": adapter,
        "session": session,
        "conversation": conversation,
        "config": config,
        "make_agent": _make_agent,
    }
    await engine.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_agent_completes_on_text_only_response(stack) -> None:
    stack["adapter"].queue_response(text_response("Design complete."))
    agent = stack["make_agent"]()

    result = await agent.run("Design the login flow.")

    assert result.outcome == AgentOutcome.COMPLETED
    assert result.final_assistant_message == "Design complete."
    assert result.tool_calls_made == 0
    assert result.total_prompt_tokens > 0


async def test_agent_dispatches_tool_then_completes(stack) -> None:
    adapter = stack["adapter"]
    adapter.queue_response(
        tool_call_response(
            [ToolCall(id="c1", name="echo", arguments={"text": "hello"})]
        )
    )
    adapter.queue_response(text_response("Heard you echo back."))
    agent = stack["make_agent"]()

    result = await agent.run("Echo hello.")

    assert result.outcome == AgentOutcome.COMPLETED
    assert result.tool_calls_made == 1
    assert result.final_assistant_message == "Heard you echo back."


async def test_agent_records_turns_and_messages(stack) -> None:
    adapter = stack["adapter"]
    adapter.queue_response(
        tool_call_response(
            [ToolCall(id="c1", name="echo", arguments={"text": "foo"})]
        )
    )
    adapter.queue_response(text_response("done"))
    agent = stack["make_agent"]()
    await agent.run("go")

    refreshed_conv = await stack["manager"].get_conversation(
        stack["conversation"].id
    )
    assert refreshed_conv is not None
    assert refreshed_conv.turn_count == 2
    assert refreshed_conv.tool_call_count == 1
    # Messages persisted: user, assistant-with-toolcall, tool, assistant-final
    persisted_roles = [m.role for m in refreshed_conv.messages]
    assert persisted_roles == ["user", "assistant", "tool", "assistant"]


async def test_agent_hits_max_turns(stack) -> None:
    """If the LLM only ever requests tool calls, max_turns terminates the loop."""
    adapter = stack["adapter"]
    for _ in range(20):
        adapter.queue_response(
            tool_call_response(
                [ToolCall(id="cX", name="echo", arguments={"text": "loop"})]
            )
        )
    agent = stack["make_agent"]()
    result = await agent.run("loop forever")
    assert result.outcome == AgentOutcome.MAX_TURNS_HIT
    assert result.final_assistant_message is None
    assert result.tool_calls_made == stack["config"].agent.max_turns


async def test_agent_retries_on_rate_limit_then_succeeds(stack) -> None:
    """Adapter raises rate-limit once, then returns success on retry."""
    adapter = stack["adapter"]
    call_count = {"n": 0}

    original_complete = adapter.complete

    async def flaky_complete(**kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise LLMRateLimitError(
                "rate limited",
                provider="mock",
                model="mock-model-1",
                retry_after_seconds=0.01,
            )
        return await original_complete(**kwargs)

    adapter.complete = flaky_complete  # type: ignore[assignment]
    adapter.queue_response(text_response("ok"))

    agent = stack["make_agent"]()
    result = await agent.run("go")

    assert result.outcome == AgentOutcome.COMPLETED
    assert call_count["n"] == 2  # 1 rate limit + 1 success
    assert result.final_assistant_message == "ok"


async def test_agent_propagates_invalid_tool_arguments_as_tool_error(stack) -> None:
    """Bad LLM arguments come back as tool error messages; LLM gets to react."""
    adapter = stack["adapter"]
    adapter.queue_response(
        tool_call_response(
            [ToolCall(id="c1", name="echo", arguments={"wrong_field": 1})]
        )
    )
    adapter.queue_response(text_response("oh well, I'll stop."))
    agent = stack["make_agent"]()
    result = await agent.run("test bad args")

    assert result.outcome == AgentOutcome.COMPLETED
    refreshed = await stack["manager"].get_conversation(
        stack["conversation"].id
    )
    tool_msg = next(m for m in refreshed.messages if m.role == "tool")
    assert "[ERROR]" in tool_msg.content
    assert "Invalid arguments" in tool_msg.content


async def test_agent_dry_run_refuses_external_writes(stack) -> None:
    adapter = stack["adapter"]
    adapter.queue_response(
        tool_call_response(
            [
                ToolCall(
                    id="c1", name="risky_write", arguments={"path": "/tmp/x"}
                )
            ]
        )
    )
    adapter.queue_response(text_response("aborted"))
    agent = stack["make_agent"](dry_run=True)
    result = await agent.run("write something")

    refreshed = await stack["manager"].get_conversation(
        stack["conversation"].id
    )
    tool_msg = next(m for m in refreshed.messages if m.role == "tool")
    assert "[REFUSED]" in tool_msg.content
    assert "dry_run" in tool_msg.content
    assert result.outcome == AgentOutcome.COMPLETED


async def test_agent_is_single_use(stack) -> None:
    adapter = stack["adapter"]
    adapter.queue_response(text_response("first"))
    agent = stack["make_agent"]()
    await agent.run("first")

    with pytest.raises(RuntimeError, match="single-use"):
        await agent.run("second")
