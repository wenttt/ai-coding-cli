"""SessionManager: owns Sessions, Conversations, Turns. See ADR-0008."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from sqlalchemy import select

from ..errors import (
    PipelineStateInconsistencyError,
    StorageIntegrityError,
)
from ..storage import (
    Conversation as ConversationORM,
)
from ..storage import (
    Session as SessionORM,
)
from ..storage import (
    StorageEngine,
    Turn as TurnORM,
)
from ._types import (
    ConversationView,
    Message,
    SessionView,
    TurnRecord,
)


def _utcnow() -> datetime:
    """Naive UTC for SQLite TIMESTAMP columns (storage stores naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_id() -> str:
    return str(uuid.uuid4())


class SessionManager:
    """Manages Session + Conversation lifecycle, backed by StorageEngine.

    All public methods are async. The manager keeps no in-memory state of its
    own: each call hits the DB. Callers can layer caching above if needed,
    but for the Lite single-user case the SQLite round-trip is well under
    1ms so a cache would be premature.
    """

    def __init__(self, storage: StorageEngine) -> None:
        self._storage = storage

    # -----------------------------------------------------------------
    # Session lifecycle
    # -----------------------------------------------------------------

    async def get_or_create_session(
        self,
        *,
        user_id: str,
        jira_key: str,
        primary_project_key: str,
        workspace_root: Path,
        mode: Literal["brownfield", "greenfield"],
        is_cross_project: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SessionView:
        """Idempotent. Returns existing Session for (user_id, jira_key) or creates one."""
        async with self._storage.session() as s:
            existing = await s.execute(
                select(SessionORM).where(
                    SessionORM.user_id == user_id, SessionORM.jira_key == jira_key
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                row.last_active_at = _utcnow()
                await s.commit()
                return _to_session_view(row)

            new = SessionORM(
                id=_new_id(),
                user_id=user_id,
                jira_key=jira_key,
                primary_project_key=primary_project_key,
                workspace_root=str(workspace_root),
                mode=mode,
                is_cross_project=is_cross_project,
                status="active",
                metadata_json=json.dumps(metadata or {}),
            )
            s.add(new)
            await s.commit()
            await s.refresh(new)
            return _to_session_view(new)

    async def get_session(self, session_id: str) -> SessionView | None:
        async with self._storage.session() as s:
            row = await s.get(SessionORM, session_id)
            return _to_session_view(row) if row else None

    async def pause_session(self, session_id: str, *, reason: str) -> None:
        async with self._storage.session() as s:
            row = await s.get(SessionORM, session_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Cannot pause unknown session {session_id!r}.",
                    session_id=session_id,
                )
            row.status = "paused"
            row.last_active_at = _utcnow()
            metadata = json.loads(row.metadata_json or "{}")
            metadata["pause_reason"] = reason
            row.metadata_json = json.dumps(metadata)
            await s.commit()

    async def resume_session(self, session_id: str) -> None:
        async with self._storage.session() as s:
            row = await s.get(SessionORM, session_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Cannot resume unknown session {session_id!r}.",
                    session_id=session_id,
                )
            row.status = "active"
            row.last_active_at = _utcnow()
            await s.commit()

    async def close_session(self, session_id: str) -> None:
        async with self._storage.session() as s:
            row = await s.get(SessionORM, session_id)
            if row is None:
                return
            row.status = "closed"
            row.closed_at = _utcnow()
            row.last_active_at = _utcnow()
            await s.commit()

    # -----------------------------------------------------------------
    # Conversation lifecycle
    # -----------------------------------------------------------------

    async def start_conversation(
        self,
        *,
        session_id: str,
        stage: str,
        revision: int = 1,
        llm_provider: str | None = None,
        llm_model: str | None = None,
    ) -> ConversationView:
        """Create a Conversation under a Session. Idempotent for (session, stage,
        revision) while the prior one is still `running`.
        """
        async with self._storage.session() as s:
            existing = await s.execute(
                select(ConversationORM).where(
                    ConversationORM.session_id == session_id,
                    ConversationORM.stage == stage,
                    ConversationORM.revision == revision,
                    ConversationORM.status == "running",
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                return _to_conversation_view(row)

            new = ConversationORM(
                id=_new_id(),
                session_id=session_id,
                stage=stage,
                revision=revision,
                status="running",
                messages_json="[]",
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
            s.add(new)
            await s.commit()
            await s.refresh(new)
            return _to_conversation_view(new)

    async def get_conversation(self, conversation_id: str) -> ConversationView | None:
        async with self._storage.session() as s:
            row = await s.get(ConversationORM, conversation_id)
            return _to_conversation_view(row) if row else None

    async def append_messages(
        self,
        conversation_id: str,
        messages: Iterable[Message],
    ) -> None:
        """Append messages to a Conversation's messages_json. Rejects if ended."""
        messages_list = list(messages)
        if not messages_list:
            return

        async with self._storage.session() as s:
            row = await s.get(ConversationORM, conversation_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Cannot append to unknown conversation {conversation_id!r}.",
                    conversation_id=conversation_id,
                )
            if row.status != "running":
                raise StorageIntegrityError(
                    f"Conversation {conversation_id!r} is {row.status!r}; "
                    "cannot append messages.",
                    conversation_id=conversation_id,
                    status=row.status,
                )

            try:
                current = json.loads(row.messages_json or "[]")
            except json.JSONDecodeError as exc:
                raise StorageIntegrityError(
                    f"Conversation {conversation_id!r} has unparseable messages_json.",
                    conversation_id=conversation_id,
                ) from exc

            current.extend(m.to_openai_dict() for m in messages_list)
            row.messages_json = json.dumps(current)
            await s.commit()

    async def overwrite_messages(
        self,
        conversation_id: str,
        messages: list[Message],
    ) -> None:
        """Replace the full message list. Used by the Compactor."""
        async with self._storage.session() as s:
            row = await s.get(ConversationORM, conversation_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Unknown conversation {conversation_id!r}.",
                    conversation_id=conversation_id,
                )
            row.messages_json = json.dumps([m.to_openai_dict() for m in messages])
            await s.commit()

    async def record_turn(self, turn: TurnRecord) -> None:
        """Insert a Turn row + bump Conversation aggregates."""
        async with self._storage.session() as s:
            row = await s.get(ConversationORM, turn.conversation_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Unknown conversation {turn.conversation_id!r}.",
                    conversation_id=turn.conversation_id,
                )
            s.add(
                TurnORM(
                    conversation_id=turn.conversation_id,
                    turn_index=turn.turn_index,
                    prompt_tokens=turn.prompt_tokens,
                    completion_tokens=turn.completion_tokens,
                    cache_hit_tokens=turn.cache_hit_tokens,
                    tool_calls_json=json.dumps(turn.tool_calls),
                    finish_reason=turn.finish_reason,
                    started_at=turn.started_at,
                    ended_at=turn.ended_at,
                    latency_seconds=turn.latency_seconds,
                )
            )
            row.turn_count = (row.turn_count or 0) + 1
            row.tool_call_count = (row.tool_call_count or 0) + len(turn.tool_calls)
            row.prompt_tokens = (row.prompt_tokens or 0) + turn.prompt_tokens
            row.completion_tokens = (row.completion_tokens or 0) + turn.completion_tokens
            row.cache_hit_tokens = (row.cache_hit_tokens or 0) + turn.cache_hit_tokens
            await s.commit()

    async def end_conversation(
        self,
        conversation_id: str,
        *,
        status: Literal["completed", "failed", "escalated"],
        operation_log_id: int | None = None,
    ) -> None:
        async with self._storage.session() as s:
            row = await s.get(ConversationORM, conversation_id)
            if row is None:
                raise PipelineStateInconsistencyError(
                    f"Unknown conversation {conversation_id!r}.",
                    conversation_id=conversation_id,
                )
            row.status = status
            row.ended_at = _utcnow()
            if operation_log_id is not None:
                row.operation_log_id = operation_log_id
            await s.commit()


# ---------------------------------------------------------------------------
# ORM-row -> view adapters
# ---------------------------------------------------------------------------


def _to_session_view(row: SessionORM) -> SessionView:
    return SessionView(
        id=row.id,
        user_id=row.user_id,
        jira_key=row.jira_key,
        primary_project_key=row.primary_project_key,
        workspace_root=Path(row.workspace_root),
        mode=row.mode,  # type: ignore[arg-type]
        is_cross_project=bool(row.is_cross_project),
        status=row.status,  # type: ignore[arg-type]
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        metadata=json.loads(row.metadata_json or "{}"),
    )


def _to_conversation_view(row: ConversationORM) -> ConversationView:
    raw_msgs = json.loads(row.messages_json or "[]")
    return ConversationView(
        id=row.id,
        session_id=row.session_id,
        stage=row.stage,
        revision=row.revision,
        status=row.status,  # type: ignore[arg-type]
        started_at=row.started_at,
        ended_at=row.ended_at,
        messages=[Message.from_openai_dict(m) for m in raw_msgs],
        turn_count=row.turn_count or 0,
        tool_call_count=row.tool_call_count or 0,
        prompt_tokens=row.prompt_tokens or 0,
        completion_tokens=row.completion_tokens or 0,
        cache_hit_tokens=row.cache_hit_tokens or 0,
        llm_provider=row.llm_provider,
        llm_model=row.llm_model,
        operation_log_id=row.operation_log_id,
    )
