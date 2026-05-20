"""Alembic environment for the Lite SQLite database.

Invoked by `ai-coding migrate up/down/status`. Reads STORAGE_DB_PATH from
the environment (or falls back to ~/.ai-coding-cli/state.db) so migrations
target the same DB the daemon uses.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from ai_coding_cli.foundation.storage._models import BASE

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = BASE.metadata


def _resolve_db_path() -> Path:
    raw = os.environ.get("STORAGE_DB_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".ai-coding-cli" / "state.db"


db_path = _resolve_db_path()
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
