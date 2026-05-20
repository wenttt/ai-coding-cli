"""Initial Lite schema: 7 SQLAlchemy tables + rag_chunks via sqlite-vec.

Revision ID: 0001_initial_lite
Revises:
Create Date: 2026-05-20

Creates:
- sessions, conversations, turns
- operation_logs_index
- processed_jira_events
- skill_invocations
- config_snapshots
- rag_chunks (sqlite-vec vec0 virtual table; 1536-dim embeddings)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_lite"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("jira_key", sa.String(64), nullable=False),
        sa.Column("primary_project_key", sa.String(64), nullable=False),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("is_cross_project", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_active_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("user_id", "jira_key", name="uq_sessions_user_jira"),
        sa.CheckConstraint(
            "mode IN ('brownfield', 'greenfield')", name="ck_sessions_mode"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'closed')", name="ck_sessions_status"
        ),
    )
    op.create_index("ix_sessions_user_status", "sessions", ["user_id", "status"])
    op.create_index("ix_sessions_jira_key", "sessions", ["jira_key"])
    op.create_index("ix_sessions_last_active", "sessions", ["last_active_at"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("messages_json", sa.Text(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_hit_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operation_log_id", sa.Integer(), nullable=True),
        sa.Column("llm_provider", sa.String(64), nullable=True),
        sa.Column("llm_model", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'escalated')",
            name="ck_conversations_status",
        ),
    )
    op.create_index("ix_conv_session", "conversations", ["session_id", "started_at"])
    op.create_index("ix_conv_stage", "conversations", ["stage", "started_at"])

    op.create_table(
        "turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_hit_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("finish_reason", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.UniqueConstraint("conversation_id", "turn_index", name="uq_turns_conv_idx"),
    )
    op.create_index("ix_turns_started", "turns", ["started_at"])

    op.create_table(
        "operation_logs_index",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("jira_key", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("skill_invoked", sa.String(128), nullable=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("outputs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("retry_context_json", sa.Text(), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("body_summary", sa.Text(), nullable=False),
        sa.Column("body_embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "jira_key", "sequence_number", "stage", "revision",
            name="uq_oplogs_key_seq_stage_rev",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'escalated')",
            name="ck_oplogs_status",
        ),
    )
    op.create_index("ix_oplogs_jira_key", "operation_logs_index", ["jira_key", "sequence_number", "revision"])
    op.create_index("ix_oplogs_stage", "operation_logs_index", ["stage", "timestamp"])
    op.create_index("ix_oplogs_session", "operation_logs_index", ["session_id"])

    op.create_table(
        "processed_jira_events",
        sa.Column("dedup_key", sa.String(128), primary_key=True),
        sa.Column("jira_key", sa.String(64), nullable=False),
        sa.Column("to_status", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("delivery_channel", sa.String(16), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "delivery_channel IN ('webhook', 'polling')",
            name="ck_jira_events_channel",
        ),
    )
    op.create_index("ix_jira_events_jira_key", "processed_jira_events", ["jira_key", "received_at"])

    op.create_table(
        "skill_invocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_name", sa.String(128), nullable=False),
        sa.Column("skill_version", sa.String(32), nullable=False),
        sa.Column("source_level", sa.String(32), nullable=False),
        sa.Column("loaded_at", sa.DateTime(), nullable=False),
        sa.Column("loaded_via", sa.String(32), nullable=False),
        sa.Column("body_tokens", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "loaded_via IN ('auto_preload', 'load_skill_tool')",
            name="ck_skill_invocations_loaded_via",
        ),
    )
    op.create_index("ix_skill_invocations_conv", "skill_invocations", ["conversation_id"])
    op.create_index("ix_skill_invocations_name", "skill_invocations", ["skill_name", "loaded_at"])

    op.create_table(
        "config_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("daemon_started_at", sa.DateTime(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.String(64), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=False),
        sa.Column("ai_coding_version", sa.String(32), nullable=False),
    )
    op.create_index("ix_config_snapshots_started", "config_snapshots", ["daemon_started_at"])

    # rag_chunks as a sqlite-vec virtual table. SQLAlchemy can't model vec0
    # cleanly, so we use raw SQL. The embedding dimension matches the default
    # embedding model (text-embedding-3-small = 1536).
    op.execute(
        """
        CREATE VIRTUAL TABLE rag_chunks USING vec0(
            chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
            embedding FLOAT[1536]
        );
        """
    )
    # Companion table for chunk metadata (sqlite-vec virtual tables can't carry
    # arbitrary columns easily; we join on chunk_id).
    op.create_table(
        "rag_chunks_meta",
        sa.Column("chunk_id", sa.Integer(), primary_key=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("indexed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "source_kind", "source_id", "chunk_index",
            name="uq_rag_meta_source_chunk",
        ),
    )
    op.create_index("ix_rag_meta_source", "rag_chunks_meta", ["source_kind", "source_id"])


def downgrade() -> None:
    op.drop_index("ix_rag_meta_source", table_name="rag_chunks_meta")
    op.drop_table("rag_chunks_meta")
    op.execute("DROP TABLE IF EXISTS rag_chunks;")

    op.drop_index("ix_config_snapshots_started", table_name="config_snapshots")
    op.drop_table("config_snapshots")

    op.drop_index("ix_skill_invocations_name", table_name="skill_invocations")
    op.drop_index("ix_skill_invocations_conv", table_name="skill_invocations")
    op.drop_table("skill_invocations")

    op.drop_index("ix_jira_events_jira_key", table_name="processed_jira_events")
    op.drop_table("processed_jira_events")

    op.drop_index("ix_oplogs_session", table_name="operation_logs_index")
    op.drop_index("ix_oplogs_stage", table_name="operation_logs_index")
    op.drop_index("ix_oplogs_jira_key", table_name="operation_logs_index")
    op.drop_table("operation_logs_index")

    op.drop_index("ix_turns_started", table_name="turns")
    op.drop_table("turns")

    op.drop_index("ix_conv_stage", table_name="conversations")
    op.drop_index("ix_conv_session", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_sessions_last_active", table_name="sessions")
    op.drop_index("ix_sessions_jira_key", table_name="sessions")
    op.drop_index("ix_sessions_user_status", table_name="sessions")
    op.drop_table("sessions")
