"""Storage Layer (Lite: SQLite + sqlite-vec). See ADR-0019 + ADR-0030.

Public exports:
    - StorageEngine: async SQLite connection wrapper with sqlite-vec
    - Database schema models (SQLAlchemy)
"""

from __future__ import annotations

from ._engine import StorageEngine, get_engine, reset_engine
from ._models import (
    BASE,
    ConfigSnapshot,
    Conversation,
    OperationLogIndex,
    ProcessedJiraEvent,
    Session,
    SkillInvocation,
    Turn,
)

__all__ = [
    "StorageEngine",
    "get_engine",
    "reset_engine",
    "BASE",
    "Session",
    "Conversation",
    "Turn",
    "OperationLogIndex",
    "ProcessedJiraEvent",
    "SkillInvocation",
    "ConfigSnapshot",
]
