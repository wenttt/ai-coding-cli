"""SessionManager unit tests. See ADR-0008."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest

from ai_coding_cli.foundation.errors import (
    PipelineStateInconsistencyError,
    StorageIntegrityError,
)
from ai_coding_cli.foundation.session import (
    Message,
    SessionManager,
    TurnRecord,
)
from ai_coding_cli.foundation.storage import BASE, StorageEngine


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    engine = StorageEngine(tmp_path / "sm.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    yield SessionManager(engine)
    await engine.close()


async def test_get_or_create_session_is_idempotent(
    manager: SessionManager, tmp_path: Path
) -> None:
    s1 = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-1",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    s2 = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-1",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    assert s1.id == s2.id
    assert s1.status == "active"


async def test_pause_and_resume_round_trip(
    manager: SessionManager, tmp_path: Path
) -> None:
    s = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-2",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="greenfield",
    )
    await manager.pause_session(s.id, reason="agent-paused label set")
    paused = await manager.get_session(s.id)
    assert paused is not None
    assert paused.status == "paused"
    assert paused.metadata.get("pause_reason") == "agent-paused label set"

    await manager.resume_session(s.id)
    resumed = await manager.get_session(s.id)
    assert resumed is not None
    assert resumed.status == "active"


async def test_start_conversation_is_idempotent_for_running(
    manager: SessionManager, tmp_path: Path
) -> None:
    s = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-3",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    c1 = await manager.start_conversation(
        session_id=s.id, stage="design", revision=1
    )
    c2 = await manager.start_conversation(
        session_id=s.id, stage="design", revision=1
    )
    assert c1.id == c2.id
    assert c1.status == "running"


async def test_append_and_overwrite_messages(
    manager: SessionManager, tmp_path: Path
) -> None:
    s = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-4",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    c = await manager.start_conversation(session_id=s.id, stage="design")

    await manager.append_messages(
        c.id,
        [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ],
    )
    refreshed = await manager.get_conversation(c.id)
    assert refreshed is not None
    assert len(refreshed.messages) == 2
    assert refreshed.messages[0].role == "user"
    assert refreshed.messages[1].content == "hello"

    await manager.overwrite_messages(
        c.id,
        [Message(role="system", content="[COMPACTED] dropped 3 results")],
    )
    after = await manager.get_conversation(c.id)
    assert after is not None
    assert len(after.messages) == 1
    assert after.messages[0].content.startswith("[COMPACTED]")


async def test_append_to_ended_conversation_rejects(
    manager: SessionManager, tmp_path: Path
) -> None:
    s = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-5",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    c = await manager.start_conversation(session_id=s.id, stage="design")
    await manager.end_conversation(c.id, status="completed")

    with pytest.raises(StorageIntegrityError):
        await manager.append_messages(c.id, [Message(role="user", content="x")])


async def test_record_turn_bumps_aggregates(
    manager: SessionManager, tmp_path: Path
) -> None:
    s = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-6",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
    )
    c = await manager.start_conversation(session_id=s.id, stage="design")
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    await manager.record_turn(
        TurnRecord(
            conversation_id=c.id,
            turn_index=0,
            prompt_tokens=100,
            completion_tokens=20,
            cache_hit_tokens=10,
            tool_calls=[{"id": "1", "name": "read_repo_file"}],
            finish_reason="tool_calls",
            started_at=now,
            ended_at=now,
            latency_seconds=0.5,
        )
    )
    refreshed = await manager.get_conversation(c.id)
    assert refreshed is not None
    assert refreshed.turn_count == 1
    assert refreshed.tool_call_count == 1
    assert refreshed.prompt_tokens == 100
    assert refreshed.completion_tokens == 20
    assert refreshed.cache_hit_tokens == 10


async def test_append_messages_unknown_conversation(manager: SessionManager) -> None:
    fake_id = str(uuid.uuid4())
    with pytest.raises(PipelineStateInconsistencyError):
        await manager.append_messages(
            fake_id, [Message(role="user", content="hi")]
        )
