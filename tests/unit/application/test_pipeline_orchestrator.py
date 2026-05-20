"""PipelineOrchestrator end-to-end test. See ADR-0003.

Strategy: build a real orchestrator backed by SQLite + SessionManager +
OperationLogWriter, register a fake StageHandler, and a fake ToolRegistry
that provides the Jira/GitHub tools the orchestrator calls (read_jira_ticket,
analyze_repo_state, transition_jira_status, add_jira_comment).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from pydantic import BaseModel

from ai_coding_cli.application.operation_log import (
    OperationLogBody,
    OperationLogReader,
    OperationLogWriter,
)
from ai_coding_cli.application.pipeline import (
    JiraStateChangeEvent,
    PipelineOrchestrator,
    PipelineStateMachine,
    StageContext,
    StageResult,
)
from ai_coding_cli.foundation.compactor import Compactor, CompactorConfig
from ai_coding_cli.foundation.config import build_test_config
from ai_coding_cli.foundation.context import ContextBuilder
from ai_coding_cli.foundation.llm import MockAdapter
from ai_coding_cli.foundation.session import SessionManager
from ai_coding_cli.foundation.storage import BASE, StorageEngine
from ai_coding_cli.foundation.tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
    tool,
)


# ---------------------------------------------------------------------------
# Module-level Pydantic arg models (PEP-563 + locals don't mix)
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


class _ReadTicketArgs(BaseModel):
    jira_key: str


class _TransitionArgs(BaseModel):
    jira_key: str
    to_status: str


class _CommentArgs(BaseModel):
    jira_key: str
    body: str


def _register_orchestrator_tools(reg: ToolRegistry, recorder: dict[str, list]) -> None:
    @tool(
        name="read_jira_ticket",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_READ,
        registry=reg,
    )
    def read_jira_ticket(args: _ReadTicketArgs, _ctx: ToolContext) -> dict[str, Any]:
        recorder.setdefault("read", []).append(args.jira_key)
        return {
            "key": args.jira_key,
            "summary": "Build OAuth login",
            "ticket_type": "user_story",
            "status": "DESIGN_DRAFTING",
            "description": "Users need OAuth login.",
        }

    @tool(
        name="analyze_repo_state",
        description="Fake.",
        side_effects=SideEffectClass.READ_ONLY,
        registry=reg,
    )
    def analyze_repo_state(_args: _NoArgs, _ctx: ToolContext) -> dict[str, Any]:
        return {
            "mode": "brownfield",
            "languages": {".py": 12},
            "has_tests": True,
            "has_ci": True,
        }

    @tool(
        name="transition_jira_status",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        visible_to_agent=False,
        registry=reg,
    )
    def transition_jira_status(args: _TransitionArgs, _ctx: ToolContext) -> dict[str, Any]:
        recorder.setdefault("transitions", []).append((args.jira_key, args.to_status))
        return {"ok": True, "to": args.to_status}

    @tool(
        name="add_jira_comment",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_WRITE,
        registry=reg,
    )
    def add_jira_comment(args: _CommentArgs, _ctx: ToolContext) -> dict[str, Any]:
        recorder.setdefault("comments", []).append((args.jira_key, args.body))
        return {"ok": True}


# ---------------------------------------------------------------------------
# Fake stage handlers
# ---------------------------------------------------------------------------


class _CompletedHandler:
    stage_name = "design"
    entry_status = "DESIGN_DRAFTING"
    exit_status_on_success = "DESIGN_REVIEW"
    exit_status_on_failure = "DESIGN_DRAFTING"
    max_retries = 3

    async def run(self, ctx: StageContext) -> StageResult:
        return StageResult(
            outcome="completed",
            summary="Design complete; Issue opened.",
            artifacts={"design_issue_url": "https://example.com/issues/1"},
            body=OperationLogBody(
                what_was_done="- Drafted design.",
                impact="Design Issue published.",
                what_i_could_not_do="_(none)_",
                engineering_decisions="- Brownfield mode.",
                next_step="Await reviewer.",
            ),
        )


class _RetryableFailureHandler:
    stage_name = "design"
    entry_status = "DESIGN_DRAFTING"
    exit_status_on_success = "DESIGN_REVIEW"
    exit_status_on_failure = "DESIGN_DRAFTING"
    max_retries = 3

    async def run(self, ctx: StageContext) -> StageResult:
        from ai_coding_cli.foundation.errors import LLMTimeoutError

        raise LLMTimeoutError(
            "fake timeout",
            provider="mock",
            model="mock-1",
            timeout_seconds=1.0,
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def orch_stack(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    engine = StorageEngine(tmp_path / "orch.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    manager = SessionManager(engine)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    writer = OperationLogWriter(engine, workspace)
    reader = OperationLogReader(engine, workspace)
    config = build_test_config(WORKSPACE_PATH=str(workspace))
    config.agent.max_turns = 5

    registry = ToolRegistry()
    recorder: dict[str, list] = {}
    _register_orchestrator_tools(registry, recorder)

    state_machine = PipelineStateMachine()

    adapter = MockAdapter()
    compactor = Compactor(adapter, CompactorConfig(preserve_recent_turns=2))
    builder = ContextBuilder()

    def _make_orchestrator() -> PipelineOrchestrator:
        return PipelineOrchestrator(
            state_machine=state_machine,
            storage=engine,
            session_manager=manager,
            operation_log_writer=writer,
            operation_log_reader=reader,
            tool_registry=registry,
            llm=adapter,
            compactor=compactor,
            context_builder=builder,
            config=config,
            primary_project_key="PROJ",
        )

    yield {
        "engine": engine,
        "manager": manager,
        "writer": writer,
        "reader": reader,
        "state_machine": state_machine,
        "registry": registry,
        "recorder": recorder,
        "workspace": workspace,
        "make_orchestrator": _make_orchestrator,
    }
    await engine.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _event(jira_key: str = "PROJ-1", to_status: str = "DESIGN_DRAFTING") -> JiraStateChangeEvent:
    return JiraStateChangeEvent(
        jira_key=jira_key,
        from_status="TODO",
        to_status=to_status,
        observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        delivery_channel="polling",
    )


async def test_react_with_no_handler_is_noop(orch_stack) -> None:
    orch = orch_stack["make_orchestrator"]()
    await orch.react(_event(to_status="CODE_REVIEW"))
    # No transitions / comments fired
    assert orch_stack["recorder"].get("transitions") in (None, [])


async def test_react_runs_handler_and_transitions_on_completed(orch_stack) -> None:
    orch_stack["state_machine"].register(_CompletedHandler())
    orch = orch_stack["make_orchestrator"]()

    await orch.react(_event())

    transitions = orch_stack["recorder"]["transitions"]
    assert ("PROJ-1", "DESIGN_REVIEW") in transitions

    comments = orch_stack["recorder"].get("comments", [])
    assert any("design_issue_url" in body for _, body in comments)

    # Operation log file + DB row exist.
    summaries = await orch_stack["reader"].list_for_ticket("PROJ-1")
    assert len(summaries) == 1
    assert summaries[0].status == "completed"
    assert summaries[0].stage == "design"


async def test_react_handles_retryable_failure(orch_stack) -> None:
    orch_stack["state_machine"].register(_RetryableFailureHandler())
    orch = orch_stack["make_orchestrator"]()

    await orch.react(_event())

    # No transition fired (retryable failure stays on current status).
    assert orch_stack["recorder"].get("transitions") in (None, [])
    # An operation log with status=failed was recorded.
    summaries = await orch_stack["reader"].list_for_ticket("PROJ-1")
    assert len(summaries) == 1
    assert summaries[0].status == "failed"


async def test_dedup_skips_repeat_events(orch_stack) -> None:
    orch_stack["state_machine"].register(_CompletedHandler())
    orch = orch_stack["make_orchestrator"]()

    event = _event()
    await orch.react(event)
    await orch.react(event)  # identical dedup_key

    transitions = orch_stack["recorder"]["transitions"]
    # Only one transition should have fired even though we reacted twice.
    assert transitions.count(("PROJ-1", "DESIGN_REVIEW")) == 1


async def test_retry_budget_escalates(orch_stack) -> None:
    """Prepopulate 3 prior failed attempts; the 4th event should escalate."""
    writer = orch_stack["writer"]

    for _ in range(3):
        await writer.write(
            jira_key="PROJ-9",
            stage="design",
            status="failed",
            agent="direct",
            skill_invoked=None,
            duration_seconds=1.0,
            inputs={},
            outputs={},
            body=OperationLogBody(
                what_was_done="prior attempt",
                impact="_(none)_",
                what_i_could_not_do="_(none)_",
                engineering_decisions="_(none)_",
                next_step="_(none)_",
            ),
        )

    orch_stack["state_machine"].register(_CompletedHandler())
    orch = orch_stack["make_orchestrator"]()

    await orch.react(_event(jira_key="PROJ-9"))

    summaries = await orch_stack["reader"].list_for_ticket("PROJ-9")
    statuses = [s.status for s in summaries]
    # 3 prior failures + 1 ESCALATED entry
    assert statuses.count("escalated") == 1
    assert any(s.status == "escalated" for s in summaries)
