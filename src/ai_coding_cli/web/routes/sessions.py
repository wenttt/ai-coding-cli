"""Session + Conversation detail."""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ...foundation.storage import Conversation, Session, Turn
from .._app import DashboardDeps


def register(app: FastAPI, deps: DashboardDeps, templates: Jinja2Templates) -> None:
    @app.get(
        "/sessions/{session_id}",
        response_class=HTMLResponse,
        name="session_detail",
    )
    async def session_detail(request: Request, session_id: str) -> HTMLResponse:
        async with deps.storage.session() as s:
            session_row = await s.get(Session, session_id)
            if session_row is None:
                raise HTTPException(status_code=404, detail="session not found")
            conversations = (
                await s.execute(
                    select(Conversation)
                    .where(Conversation.session_id == session_id)
                    .order_by(Conversation.started_at)
                )
            ).scalars().all()
        return templates.TemplateResponse(
            request,
            "session_detail.html",
            {
                "session": session_row,
                "conversations": conversations,
            },
        )

    @app.get(
        "/conversations/{conversation_id}",
        response_class=HTMLResponse,
        name="conversation_detail",
    )
    async def conversation_detail(
        request: Request, conversation_id: str
    ) -> HTMLResponse:
        async with deps.storage.session() as s:
            conv_row = await s.get(Conversation, conversation_id)
            if conv_row is None:
                raise HTTPException(status_code=404, detail="conversation not found")
            turn_rows = (
                await s.execute(
                    select(Turn)
                    .where(Turn.conversation_id == conversation_id)
                    .order_by(Turn.turn_index)
                )
            ).scalars().all()

        messages = _safe_json_load(conv_row.messages_json, default=[])
        return templates.TemplateResponse(
            request,
            "conversation_detail.html",
            {
                "conversation": conv_row,
                "messages": messages,
                "turns": turn_rows,
            },
        )


def _safe_json_load(text: str, *, default):  # type: ignore[no-untyped-def]
    try:
        return json.loads(text or "")
    except (json.JSONDecodeError, ValueError):
        return default
