"""Operation log detail view."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...foundation.errors import OperationLogIntegrityError
from .._app import DashboardDeps


def register(app: FastAPI, deps: DashboardDeps, templates: Jinja2Templates) -> None:
    @app.get(
        "/operation_logs/{log_id}",
        response_class=HTMLResponse,
        name="operation_log_detail",
    )
    async def operation_log_detail(request: Request, log_id: int) -> HTMLResponse:
        try:
            full = await deps.operation_log_reader.read_full(log_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="operation log not found")
        except OperationLogIntegrityError as exc:
            return templates.TemplateResponse(
                request,
                "operation_log_integrity_error.html",
                {"error": str(exc)},
                status_code=409,
            )
        return templates.TemplateResponse(
            request,
            "operation_log_detail.html",
            {
                "frontmatter": full.frontmatter,
                "body": full.body,
            },
        )
