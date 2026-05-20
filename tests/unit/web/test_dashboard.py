"""Web Dashboard smoke tests. See ADR-0026."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from ai_coding_cli.application.operation_log import (
    OperationLogBody,
    OperationLogReader,
    OperationLogWriter,
)
from ai_coding_cli.foundation.config import build_test_config
from ai_coding_cli.foundation.session import SessionManager
from ai_coding_cli.foundation.storage import BASE, StorageEngine
from ai_coding_cli.web import DashboardDeps, build_dashboard_app


@pytest.fixture
async def deps(tmp_path: Path) -> AsyncIterator[DashboardDeps]:
    engine = StorageEngine(tmp_path / "dash.db")
    async with engine._async_engine.begin() as conn:  # noqa: SLF001
        await conn.run_sync(BASE.metadata.create_all)
    manager = SessionManager(engine)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    writer = OperationLogWriter(engine, workspace)

    # Seed: one session + one operation log
    session = await manager.get_or_create_session(
        user_id="me",
        jira_key="PROJ-DASH",
        primary_project_key="PROJ",
        workspace_root=workspace,
        mode="brownfield",
    )
    await manager.start_conversation(session_id=session.id, stage="design")
    await writer.write(
        jira_key="PROJ-DASH",
        stage="design",
        status="completed",
        agent="direct",
        skill_invoked=None,
        duration_seconds=1.0,
        inputs={"from_status": "TODO"},
        outputs={"design_issue_url": "https://example.com/issues/1"},
        body=OperationLogBody(
            what_was_done="Drafted design.",
            impact="Design Issue opened.",
            what_i_could_not_do="_(none)_",
            engineering_decisions="Brownfield mode.",
            next_step="Await reviewer.",
        ),
    )

    config = build_test_config(WORKSPACE_PATH=str(workspace))
    yield DashboardDeps(
        config=config,
        storage=engine,
        session_manager=manager,
        operation_log_reader=OperationLogReader(engine, workspace),
    )
    await engine.close()


def test_home_renders(deps: DashboardDeps) -> None:
    app = build_dashboard_app(deps)
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "PROJ-DASH" in r.text


def test_tickets_list_renders(deps: DashboardDeps) -> None:
    app = build_dashboard_app(deps)
    with TestClient(app) as client:
        r = client.get("/tickets")
    assert r.status_code == 200
    assert "PROJ-DASH" in r.text


def test_ticket_detail_renders_with_logs(deps: DashboardDeps) -> None:
    app = build_dashboard_app(deps)
    with TestClient(app) as client:
        r = client.get("/tickets/PROJ-DASH")
    assert r.status_code == 200
    assert "Operation log timeline" in r.text
    assert "design" in r.text


def test_ticket_detail_404_for_unknown(deps: DashboardDeps) -> None:
    app = build_dashboard_app(deps)
    with TestClient(app) as client:
        r = client.get("/tickets/UNKNOWN-1")
    assert r.status_code == 404


def test_health_returns_ok(deps: DashboardDeps) -> None:
    app = build_dashboard_app(deps)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] in ("ok", "degraded")
    assert payload["sqlite"] in ("ok", "unavailable")
    assert "ai_coding_version" in payload
