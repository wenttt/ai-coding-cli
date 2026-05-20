"""Ticket detail view: operation log timeline + conversations."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ...foundation.storage import Conversation, Session
from .._app import DashboardDeps


def register(app: FastAPI, deps: DashboardDeps, templates: Jinja2Templates) -> None:
    @app.get("/tickets", response_class=HTMLResponse, name="tickets_list")
    async def tickets_list(request: Request) -> HTMLResponse:
        async with deps.storage.session() as s:
            session_rows = (
                await s.execute(select(Session).order_by(Session.last_active_at.desc()))
            ).scalars().all()
        return templates.TemplateResponse(
            request,
            "tickets_list.html",
            {"sessions": session_rows},
        )

    @app.get(
        "/tickets/{jira_key}",
        response_class=HTMLResponse,
        name="ticket_detail",
    )
    async def ticket_detail(request: Request, jira_key: str) -> HTMLResponse:
        async with deps.storage.session() as s:
            session_row = (
                await s.execute(
                    select(Session).where(Session.jira_key == jira_key)
                )
            ).scalar_one_or_none()
            if session_row is None:
                # Allow viewing a ticket even when no Session has been opened
                # yet (operation logs may exist from manual_invoke).
                conversations: list[Conversation] = []
            else:
                conversations = (
                    await s.execute(
                        select(Conversation)
                        .where(Conversation.session_id == session_row.id)
                        .order_by(Conversation.started_at)
                    )
                ).scalars().all()

        logs = await deps.operation_log_reader.list_for_ticket(jira_key)
        if not logs and session_row is None:
            raise HTTPException(status_code=404, detail=f"No data for {jira_key!r}")

        return templates.TemplateResponse(
            request,
            "ticket_detail.html",
            {
                "jira_key": jira_key,
                "session": session_row,
                "conversations": conversations,
                "logs": logs,
            },
        )
