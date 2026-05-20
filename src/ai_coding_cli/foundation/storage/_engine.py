"""StorageEngine: async SQLite connection wrapper with sqlite-vec extension."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import sqlite_vec
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..errors import StorageSqliteUnavailable

# Module-level singleton; lazily initialised by get_engine().
_engine: "StorageEngine | None" = None


class StorageEngine:
    """Async SQLite engine with sqlite-vec loaded on every new connection.

    SQLAlchemy 2.x async + aiosqlite driver. WAL journaling is enabled at
    connect time so the daemon can read while a writer holds the lock.

    Use:
        async with engine.session() as s:
            row = await s.execute(...)
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._async_engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
            },
        )

        # Attach sqlite-vec + WAL on each new physical connection.
        # SQLAlchemy's aiosqlite adapter exposes `driver_connection`
        # (the aiosqlite.Connection) plus an `await_` helper that bridges
        # this synchronous listener into the aiosqlite worker thread, which is
        # the only thread allowed to touch the underlying sqlite3 connection.
        @event_compat(self._async_engine.sync_engine)
        def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            aio_conn = dbapi_conn.driver_connection
            try:
                dbapi_conn.await_(aio_conn.enable_load_extension(True))
                dbapi_conn.await_(aio_conn.load_extension(sqlite_vec.loadable_path()))
                dbapi_conn.await_(aio_conn.enable_load_extension(False))
            except Exception as exc:  # pragma: no cover - depends on platform build
                raise StorageSqliteUnavailable(
                    "Failed to load sqlite-vec extension; check that the wheel matches your platform.",
                    cause=exc,
                    db_path=str(db_path),
                ) from exc
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        self._sessionmaker = async_sessionmaker(
            self._async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @property
    def db_path(self) -> Path:
        return self._db_path

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield an AsyncSession with transaction handling.

        The session is closed automatically. Callers control commit / rollback
        explicitly via `await s.commit()` or by letting an exception propagate.
        """
        async with self._sessionmaker() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    async def ping(self) -> None:
        """Validate the connection works. Raises StorageSqliteUnavailable on failure."""
        try:
            async with self._async_engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            raise StorageSqliteUnavailable(
                f"SQLite database at {self._db_path!s} is not reachable.",
                cause=exc,
                db_path=str(self._db_path),
            ) from exc

    async def close(self) -> None:
        await self._async_engine.dispose()


def event_compat(sync_engine):  # type: ignore[no-untyped-def]
    """Return a decorator equivalent to SQLAlchemy's @event.listens_for(engine, 'connect').

    Wrapped here so the StorageEngine module doesn't import the global
    `event` namespace eagerly.
    """
    from sqlalchemy import event

    def _decorator(fn):  # type: ignore[no-untyped-def]
        event.listen(sync_engine, "connect", fn)
        return fn

    return _decorator


def get_engine(db_path: Path | None = None) -> StorageEngine:
    """Return the process-wide StorageEngine, constructing if necessary."""
    global _engine
    if _engine is None:
        if db_path is None:
            raise ValueError("First call to get_engine() must supply db_path.")
        _engine = StorageEngine(db_path)
    return _engine


def reset_engine() -> None:
    """Tests only. Reset the global singleton."""
    global _engine
    _engine = None
