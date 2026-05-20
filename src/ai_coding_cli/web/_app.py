"""FastAPI app factory for the local Dashboard. See ADR-0026."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..application.operation_log import OperationLogReader
from ..foundation.config import Config
from ..foundation.session import SessionManager
from ..foundation.storage import StorageEngine

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


@dataclass(frozen=True)
class DashboardDeps:
    """Dependency injection bundle for the Dashboard."""

    config: Config
    storage: StorageEngine
    session_manager: SessionManager
    operation_log_reader: OperationLogReader


def build_dashboard_app(deps: DashboardDeps) -> FastAPI:
    """Construct the FastAPI app. The returned app is ready to be served by
    uvicorn (the daemon does so on 127.0.0.1).
    """
    app = FastAPI(
        title="ai-coding-cli Dashboard",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Register route modules. Each module exports `register(app, deps, templates)`.
    from .routes import health, home, operation_logs, sessions, tickets

    home.register(app, deps, templates)
    tickets.register(app, deps, templates)
    sessions.register(app, deps, templates)
    operation_logs.register(app, deps, templates)
    health.register(app, deps)

    return app
