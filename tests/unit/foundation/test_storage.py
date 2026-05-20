"""Storage smoke tests. See ADR-0019.

Boots a real SQLite file, runs ORM-driven create_all, inserts + selects rows,
exercises WAL pragma + sqlite-vec extension. Alembic migrations themselves
are exercised in tests/integration/.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
from sqlalchemy import select, text

from ai_coding_cli.foundation.storage import (
    BASE,
    Conversation,
    Session,
    StorageEngine,
    Turn,
)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[StorageEngine]:
    db_path = tmp_path / "test_state.db"
    eng = StorageEngine(db_path)
    async with eng._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    yield eng
    await eng.close()


async def test_engine_pings_successfully(engine: StorageEngine) -> None:
    await engine.ping()


async def test_wal_journal_mode_enabled(engine: StorageEngine) -> None:
    async with engine.session() as s:
        result = await s.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()
    assert str(mode).lower() == "wal"


async def test_foreign_keys_pragma_enabled(engine: StorageEngine) -> None:
    async with engine.session() as s:
        result = await s.execute(text("PRAGMA foreign_keys"))
        flag = result.scalar()
    assert flag == 1


async def test_sqlite_vec_loaded(engine: StorageEngine) -> None:
    async with engine.session() as s:
        result = await s.execute(text("SELECT vec_version()"))
        version = result.scalar()
    assert isinstance(version, str) and version.startswith("v")


def _make_session(**overrides: object) -> Session:
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "user_id": "test-user",
        "jira_key": "PROJ-1",
        "primary_project_key": "PROJ",
        "workspace_root": "/tmp/ws",
        "mode": "greenfield",
        "is_cross_project": False,
        "status": "active",
    }
    defaults.update(overrides)
    return Session(**defaults)  # type: ignore[arg-type]


def _make_conversation(session_id: str, **overrides: object) -> Conversation:
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "stage": "design",
        "revision": 1,
        "status": "running",
        "messages_json": "[]",
    }
    defaults.update(overrides)
    return Conversation(**defaults)  # type: ignore[arg-type]


def _make_turn(conversation_id: str, *, turn_index: int = 0) -> Turn:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Turn(
        conversation_id=conversation_id,
        turn_index=turn_index,
        prompt_tokens=10,
        completion_tokens=5,
        finish_reason="stop",
        started_at=now,
        ended_at=now,
        latency_seconds=0.1,
    )


async def test_session_conversation_turn_round_trip(engine: StorageEngine) -> None:
    sess = _make_session()
    conv = _make_conversation(sess.id)
    turn = _make_turn(conv.id, turn_index=0)

    async with engine.session() as s:
        s.add(sess)
        s.add(conv)
        s.add(turn)
        await s.commit()

    async with engine.session() as s:
        result = await s.execute(
            select(Turn).where(Turn.conversation_id == conv.id)
        )
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].turn_index == 0
    assert rows[0].finish_reason == "stop"


async def test_rollback_on_exception(engine: StorageEngine) -> None:
    sess = _make_session(jira_key="PROJ-ROLLBACK")
    sess_id = sess.id

    with pytest.raises(RuntimeError, match="boom"):
        async with engine.session() as s:
            s.add(sess)
            await s.flush()
            raise RuntimeError("boom")

    async with engine.session() as s:
        result = await s.execute(select(Session).where(Session.id == sess_id))
        row = result.scalar_one_or_none()
    assert row is None
