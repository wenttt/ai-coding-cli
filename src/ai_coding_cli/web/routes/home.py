"""Dashboard home view: tickets in flight + recent activity."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from ...foundation.storage import OperationLogIndex, Session
from .._app import DashboardDeps


def register(app: FastAPI, deps: DashboardDeps, templates: Jinja2Templates) -> None:
    @app.get("/", response_class=HTMLResponse, name="home")
    async def home(request: Request) -> HTMLResponse:
        async with deps.storage.session() as s:
            session_rows = (
                await s.execute(
                    select(Session).order_by(desc(Session.last_active_at)).limit(20)
                )
            ).scalars().all()
            recent_logs = (
                await s.execute(
                    select(OperationLogIndex)
                    .order_by(desc(OperationLogIndex.timestamp))
                    .limit(20)
                )
            ).scalars().all()

        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "sessions": session_rows,
                "recent_logs": recent_logs,
                "primary_project_key": _detect_primary_project_key(deps),
            },
        )


def _detect_primary_project_key(deps: DashboardDeps) -> str | None:
    # In Lite the Config doesn't expose a top-level primary_project_key; the
    # orchestrator owns it. The Dashboard falls back to "(unset)" when absent.
    return None
