"""SQLAlchemy models for the Lite SQLite schema. See ADR-0019 + ADR-0030.

Lite-only subset: sessions, conversations, turns, operation_logs_index,
rag_chunks, processed_jira_events, skill_invocations, config_snapshots.

memory_entries, memory_governance_log, neo4j_outbox are reserved for the
Standard profile and not created in Lite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid_str() -> str:
    return str(uuid.uuid4())


class BASE(DeclarativeBase):  # type: ignore[misc]
    """Declarative base for all Lite storage tables."""


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session(BASE):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    jira_key: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_project_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_root: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    is_cross_project: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "jira_key", name="uq_sessions_user_jira"),
        CheckConstraint(
            "mode IN ('brownfield', 'greenfield')", name="ck_sessions_mode"
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'closed')", name="ck_sessions_status"
        ),
        Index("ix_sessions_user_status", "user_id", "status"),
        Index("ix_sessions_jira_key", "jira_key"),
        Index("ix_sessions_last_active", "last_active_at"),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class Conversation(BASE):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operation_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    session: Mapped["Session"] = relationship(back_populates="conversations")
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'escalated')",
            name="ck_conversations_status",
        ),
        Index("ix_conv_session", "session_id", "started_at"),
        Index("ix_conv_stage", "stage", "started_at"),
    )


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


class Turn(BASE):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_hit_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    finish_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="turns")

    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index", name="uq_turns_conv_idx"),
        Index("ix_turns_started", "started_at"),
    )


# ---------------------------------------------------------------------------
# Operation log index (body lives in Markdown files on disk per ADR-0005)
# ---------------------------------------------------------------------------


class OperationLogIndex(BASE):
    __tablename__ = "operation_logs_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    jira_key: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    skill_invoked: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    outputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    retry_context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_summary: Mapped[str] = mapped_column(Text, nullable=False)
    body_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "jira_key", "sequence_number", "stage", "revision",
            name="uq_oplogs_key_seq_stage_rev",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed', 'escalated')",
            name="ck_oplogs_status",
        ),
        Index("ix_oplogs_jira_key", "jira_key", "sequence_number", "revision"),
        Index("ix_oplogs_stage", "stage", "timestamp"),
        Index("ix_oplogs_session", "session_id"),
    )


# ---------------------------------------------------------------------------
# Processed Jira events (idempotency for polling)
# ---------------------------------------------------------------------------


class ProcessedJiraEvent(BASE):
    __tablename__ = "processed_jira_events"

    dedup_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    jira_key: Mapped[str] = mapped_column(String(64), nullable=False)
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    delivery_channel: Mapped[str] = mapped_column(String(16), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "delivery_channel IN ('webhook', 'polling')",
            name="ck_jira_events_channel",
        ),
        Index("ix_jira_events_jira_key", "jira_key", "received_at"),
    )


# ---------------------------------------------------------------------------
# Skill invocations
# ---------------------------------------------------------------------------


class SkillInvocation(BASE):
    __tablename__ = "skill_invocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    skill_name: Mapped[str] = mapped_column(String(128), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_level: Mapped[str] = mapped_column(String(32), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    loaded_via: Mapped[str] = mapped_column(String(32), nullable=False)
    body_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "loaded_via IN ('auto_preload', 'load_skill_tool')",
            name="ck_skill_invocations_loaded_via",
        ),
        Index("ix_skill_invocations_conv", "conversation_id"),
        Index("ix_skill_invocations_name", "skill_name", "loaded_at"),
    )


# ---------------------------------------------------------------------------
# Config snapshots (audit of daemon startups)
# ---------------------------------------------------------------------------


class ConfigSnapshot(BASE):
    __tablename__ = "config_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    daemon_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_coding_version: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_config_snapshots_started", "daemon_started_at"),
    )


# Note on rag_chunks:
# The rag_chunks table stores 1536-dim float32 vectors via the sqlite-vec
# extension's vec0 virtual table type. SQLAlchemy can't model virtual tables
# cleanly, so it's created via raw SQL in the Alembic migration.
