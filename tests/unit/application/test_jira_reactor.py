"""JiraReactor unit tests. See ADR-0029."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from ai_coding_cli.application.jira_reaction import JiraReactor, JiraReactorConfig
from ai_coding_cli.application.pipeline import JiraStateChangeEvent
from ai_coding_cli.foundation.config import build_test_config
from ai_coding_cli.foundation.tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
    tool,
)


class _NoArgs(BaseModel):
    pass


def _make_registry(tickets: list[dict[str, Any]]) -> ToolRegistry:
    reg = ToolRegistry()

    @tool(
        name="list_my_tickets",
        description="Fake.",
        side_effects=SideEffectClass.EXTERNAL_READ,
        registry=reg,
    )
    def list_my_tickets(_args: _NoArgs, _ctx: ToolContext) -> list[dict[str, Any]]:
        return list(tickets)

    return reg


async def test_poll_once_emits_event_for_new_ticket(tmp_path) -> None:
    tickets = [
        {
            "key": "PROJ-1",
            "status": "DESIGN_DRAFTING",
            "updated": "2026-05-20T12:00:00Z",
        }
    ]
    registry = _make_registry(tickets)
    config = build_test_config(WORKSPACE_PATH=str(tmp_path))

    orch = AsyncMock()
    reactor = JiraReactor(
        orchestrator=orch,
        tool_registry=registry,
        config=config,
        reactor_config=JiraReactorConfig(),
    )

    events = await reactor.poll_once()
    assert len(events) == 1
    assert events[0].jira_key == "PROJ-1"
    assert events[0].to_status == "DESIGN_DRAFTING"
    assert events[0].delivery_channel == "polling"
    orch.react.assert_awaited_once()


async def test_poll_once_is_idempotent_on_unchanged_status(tmp_path) -> None:
    tickets = [
        {
            "key": "PROJ-1",
            "status": "DESIGN_DRAFTING",
            "updated": "2026-05-20T12:00:00Z",
        }
    ]
    registry = _make_registry(tickets)
    config = build_test_config(WORKSPACE_PATH=str(tmp_path))
    orch = AsyncMock()
    reactor = JiraReactor(
        orchestrator=orch,
        tool_registry=registry,
        config=config,
    )

    first = await reactor.poll_once()
    second = await reactor.poll_once()
    assert len(first) == 1
    assert len(second) == 0
    orch.react.assert_awaited_once()


async def test_poll_once_emits_event_on_status_change(tmp_path) -> None:
    tickets = [
        {
            "key": "PROJ-1",
            "status": "DESIGN_DRAFTING",
            "updated": "2026-05-20T12:00:00Z",
        }
    ]
    registry = _make_registry(tickets)
    config = build_test_config(WORKSPACE_PATH=str(tmp_path))
    orch = AsyncMock()
    reactor = JiraReactor(
        orchestrator=orch,
        tool_registry=registry,
        config=config,
    )
    await reactor.poll_once()

    # Status changes; updated timestamp advances.
    tickets[0]["status"] = "DESIGN_REVIEW"
    tickets[0]["updated"] = "2026-05-20T13:00:00Z"

    events = await reactor.poll_once()
    assert len(events) == 1
    assert events[0].from_status == "DESIGN_DRAFTING"
    assert events[0].to_status == "DESIGN_REVIEW"


async def test_dedup_key_is_deterministic_per_status_and_timestamp() -> None:
    from datetime import datetime

    e1 = JiraStateChangeEvent(
        jira_key="PROJ-1",
        from_status="TODO",
        to_status="DESIGN_DRAFTING",
        observed_at=datetime(2026, 5, 20, 12, 0, 0),
        delivery_channel="polling",
    )
    e2 = JiraStateChangeEvent(
        jira_key="PROJ-1",
        from_status="TODO",  # differs from None but shouldn't affect dedup
        to_status="DESIGN_DRAFTING",
        observed_at=datetime(2026, 5, 20, 12, 0, 0),
        delivery_channel="webhook",
    )
    assert e1.dedup_key == e2.dedup_key


async def test_poll_once_handles_empty_tickets(tmp_path) -> None:
    registry = _make_registry([])
    config = build_test_config(WORKSPACE_PATH=str(tmp_path))
    orch = AsyncMock()
    reactor = JiraReactor(
        orchestrator=orch,
        tool_registry=registry,
        config=config,
    )
    events = await reactor.poll_once()
    assert events == []
    orch.react.assert_not_awaited()
