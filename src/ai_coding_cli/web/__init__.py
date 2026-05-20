"""Local Web Dashboard. See ADR-0026 + ADR-0030.

Lite scope:
    - Read-only views over operation logs / sessions / tickets / health
    - FastAPI + Jinja2 + Tailwind CDN; no HTMX / WebSocket
    - 127.0.0.1 binding only; no auth

Public exports:
    - build_dashboard_app(deps) -> FastAPI
    - DashboardDeps: dependency injection bundle
"""

from __future__ import annotations

from ._app import DashboardDeps, build_dashboard_app

__all__ = [
    "DashboardDeps",
    "build_dashboard_app",
]
