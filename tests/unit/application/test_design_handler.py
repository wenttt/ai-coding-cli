"""BrownfieldDesignHandler test. See ADR-0004.

Strategy: build a full stack (StorageEngine + SessionManager + ToolRegistry
seeded with a fake `create_design_issue` tool + Agent driving a scripted
MockAdapter). Run the handler, assert the StageResult + persisted state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from ai_coding_cli.application.pipeline._context import StageContext
from ai_coding_cli.application.pipeline.stages.design import (
    BrownfieldDesignHandler,
)
from ai_coding_cli.foundation.agent import Agent
from ai_coding_cli.foundation.compactor import Compactor, CompactorConfig
from ai_coding_cli.foundation.config import build_test_config
from ai_coding_cli.foundation.context import ContextBuilder, RepoFacts
from ai_coding_cli.foundation.llm import MockAdapter
from ai_coding_cli.foundation.llm._adapter import ToolCall
from ai_coding_cli.foundation.llm._mock import (
    text_response,
    tool_call_response,
)
from ai_coding_cli.foundation.session import SessionManager
from ai_coding_cli.foundation.storage import BASE, StorageEngine
from ai_coding_cli.foundation.tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
    tool,
)


# ---------------------------------------------------------------------------
# Fake tool inputs
# ---------------------------------------------------------------------------


class _CreateIssueArgs(BaseModel):
    jira_key: str
    title: str
    body: str
    labels: list[str] | None = None


class _CommentArgs(BaseModel):
    jira_key: str
    body: str


class _FindIssueArgs(BaseModel):
    jira_key: str


def _register_design_tools(reg: ToolRegistry, recorder: dict[str, list]) -> None:
    @tool(
        name="find_design_issue_for_jira",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_READ,
        registry=reg,
    )
    def find_design_issue_for_jira(args: _FindIssueArgs, _ctx: ToolContext) -> dict[str, Any] | None:
        recorder.setdefault("find", []).append(args.jira_key)
        return None

    @tool(
        name="create_design_issue",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        registry=reg,
    )
    def create_design_issue(args: _CreateIssueArgs, _ctx: ToolContext) -> dict[str, Any]:
        recorder.setdefault("create", []).append(
            {"jira_key": args.jira_key, "title": args.title}
        )
        return {
            "number": 42,
            "html_url": "https://github.com/org/repo/issues/42",
            "title": args.title,
        }

    @tool(
        name="add_jira_comment",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        registry=reg,
    )
    def add_jira_comment(args: _CommentArgs, _ctx: ToolContext) -> dict[str, Any]:
        recorder.setdefault("comment", []).append((args.jira_key, args.body))
        return {"ok": True}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def design_stack(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    engine = StorageEngine(tmp_path / "design.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    manager = SessionManager(engine)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = build_test_config(WORKSPACE_PATH=str(workspace))
    config.agent.max_turns = 8

    registry = ToolRegistry()
    recorder: dict[str, list] = {}
    _register_design_tools(registry, recorder)

    session = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-DESIGN",
        primary_project_key="PROJ",
        workspace_root=workspace,
        mode="brownfield",
    )
    conversation = await manager.start_conversation(
        session_id=session.id, stage="design"
    )

    adapter = MockAdapter()
    compactor = Compactor(adapter, CompactorConfig(preserve_recent_turns=2))
    builder = ContextBuilder()

    def _make_agent() -> Agent:
        return Agent(
            session=session,
            conversation=conversation,
            llm=adapter,
            tools=registry,
            context_builder=builder,
            compactor=compactor,
            session_manager=manager,
            config=config,
            repo_facts=RepoFacts(languages=["Python"]),
            conventions=None,
            loaded_skills=[],
            operation_log_path=None,
        )

    yield {
        "manager": manager,
        "registry": registry,
        "recorder": recorder,
        "adapter": adapter,
        "session": session,
        "conversation": conversation,
        "config": config,
        "make_agent": _make_agent,
        "workspace": workspace,
    }
    await engine.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_brownfield_handler_completes_when_issue_created(design_stack) -> None:
    adapter = design_stack["adapter"]
    # Script the agent: 1) find_design_issue_for_jira -> 2) create_design_issue ->
    # 3) add_jira_comment -> 4) final STAGE_RESULT message.
    adapter.queue_response(
        tool_call_response(
            [
                ToolCall(
                    id="c1",
                    name="find_design_issue_for_jira",
                    arguments={"jira_key": "PROJ-DESIGN"},
                )
            ]
        )
    )
    adapter.queue_response(
        tool_call_response(
            [
                ToolCall(
                    id="c2",
                    name="create_design_issue",
                    arguments={
                        "jira_key": "PROJ-DESIGN",
                        "title": "[PROJ-DESIGN] Design: Build OAuth login",
                        "body": "---\njira_key: PROJ-DESIGN\n...\n---\n\nfull body",
                        "labels": ["jira:proj-design", "stage:design"],
                    },
                )
            ]
        )
    )
    adapter.queue_response(
        tool_call_response(
            [
                ToolCall(
                    id="c3",
                    name="add_jira_comment",
                    arguments={
                        "jira_key": "PROJ-DESIGN",
                        "body": "Design Issue: https://github.com/org/repo/issues/42",
                    },
                )
            ]
        )
    )
    adapter.queue_response(
        text_response(
            "STAGE_RESULT\n"
            "outcome: completed\n"
            "design_issue_url: https://github.com/org/repo/issues/42\n"
            "design_issue_number: 42\n"
            "risk_level: medium\n"
            "summary: Drafted brownfield design; ready for review.\n"
        )
    )

    handler = BrownfieldDesignHandler()
    ctx = StageContext(
        jira_key="PROJ-DESIGN",
        jira_ticket={
            "key": "PROJ-DESIGN",
            "summary": "Build OAuth login",
            "ticket_type": "user_story",
            "status": "DESIGN_DRAFTING",
            "description": "Users need OAuth login.",
        },
        prior_logs=[],
        retry_count=0,
        session=design_stack["session"],
        conversation=design_stack["conversation"],
        agent=design_stack["make_agent"](),
        workspace_root=design_stack["workspace"],
        mode="brownfield",
        is_cross_project=False,
        delivery_channel="polling",
    )

    result = await handler.run(ctx)
    assert result.outcome == "completed"
    assert result.artifacts["design_issue_url"] == "https://github.com/org/repo/issues/42"
    assert result.artifacts.get("design_issue_number") == "42"
    assert result.artifacts.get("risk_level") == "medium"
    assert result.body is not None
    assert "Design Issue" in result.body.what_was_done


async def test_brownfield_handler_fails_when_no_issue_created(design_stack) -> None:
    adapter = design_stack["adapter"]
    # Agent gives up immediately without creating an issue.
    adapter.queue_response(
        text_response(
            "STAGE_RESULT\noutcome: failed\nsummary: Ticket too vague to design.\n"
        )
    )
    handler = BrownfieldDesignHandler()
    ctx = StageContext(
        jira_key="PROJ-DESIGN",
        jira_ticket={
            "key": "PROJ-DESIGN",
            "summary": "Something",
            "ticket_type": "task",
            "status": "DESIGN_DRAFTING",
            "description": "",
        },
        prior_logs=[],
        retry_count=0,
        session=design_stack["session"],
        conversation=design_stack["conversation"],
        agent=design_stack["make_agent"](),
        workspace_root=design_stack["workspace"],
        mode="brownfield",
        is_cross_project=False,
        delivery_channel="polling",
    )
    result = await handler.run(ctx)
    assert result.outcome == "failed"
    assert "Ticket too vague" in result.summary


async def test_brownfield_handler_falls_back_to_conversation_scan(design_stack) -> None:
    """Agent creates the issue but its final message is malformed; the handler
    still recovers the design_issue_url by scanning tool results."""
    adapter = design_stack["adapter"]
    adapter.queue_response(
        tool_call_response(
            [
                ToolCall(
                    id="c1",
                    name="create_design_issue",
                    arguments={
                        "jira_key": "PROJ-DESIGN",
                        "title": "[PROJ-DESIGN] Design",
                        "body": "body",
                    },
                )
            ]
        )
    )
    adapter.queue_response(text_response("done."))  # malformed final message

    handler = BrownfieldDesignHandler()
    ctx = StageContext(
        jira_key="PROJ-DESIGN",
        jira_ticket={
            "key": "PROJ-DESIGN",
            "summary": "Build OAuth login",
            "ticket_type": "user_story",
            "status": "DESIGN_DRAFTING",
            "description": "Users need OAuth login.",
        },
        prior_logs=[],
        retry_count=0,
        session=design_stack["session"],
        conversation=design_stack["conversation"],
        agent=design_stack["make_agent"](),
        workspace_root=design_stack["workspace"],
        mode="brownfield",
        is_cross_project=False,
        delivery_channel="polling",
    )
    result = await handler.run(ctx)
    assert result.outcome == "completed"
    assert result.artifacts["design_issue_url"] == "https://github.com/org/repo/issues/42"
