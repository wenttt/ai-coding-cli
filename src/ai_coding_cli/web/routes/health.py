"""Health endpoint: JSON, used by uptime monitors + the CLI doctor command."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from ... import __version__
from .._app import DashboardDeps


def register(app: FastAPI, deps: DashboardDeps) -> None:
    @app.get("/health", name="health")
    async def health() -> dict[str, str | bool]:
        sqlite_ok = True
        try:
            await deps.storage.ping()
        except Exception:  # noqa: BLE001
            sqlite_ok = False
        return {
            "status": "ok" if sqlite_ok else "degraded",
            "sqlite": "ok" if sqlite_ok else "unavailable",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "ai_coding_version": __version__,
        }
