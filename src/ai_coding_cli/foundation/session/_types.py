"""In-memory views over Session + Conversation rows. See ADR-0008.

These are the dataclasses the Agent Core and StageHandlers see. The DB-side
SQLAlchemy models live in foundation/storage/_models.py; this module hides
the ORM rows behind read-only views so consumers don't accidentally mutate
persisted state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Message: OpenAI chat-format envelope
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """One OpenAI-format message: system / user / assistant / tool.

    Stored verbatim in `Conversation.messages_json` per ADR-0008. The full
    list is the entire Short-term Memory for the Conversation.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_openai_dict(self) -> dict[str, Any]:
        """Serialize to the OpenAI chat-completions wire format."""
        out: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            out["content"] = self.content
        if self.name is not None:
            out["name"] = self.name
        if self.tool_calls is not None:
            out["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        return out

    @classmethod
    def from_openai_dict(cls, payload: dict[str, Any]) -> "Message":
        return cls(
            role=payload["role"],
            content=payload.get("content"),
            name=payload.get("name"),
            tool_calls=payload.get("tool_calls"),
            tool_call_id=payload.get("tool_call_id"),
        )


# ---------------------------------------------------------------------------
# Turn record (one ReAct iteration)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRecord:
    """Persisted statistics for one ReAct turn. See ADR-0008 + ADR-0009."""

    conversation_id: str
    turn_index: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    tool_calls: list[dict[str, Any]]
    finish_reason: str
    started_at: datetime
    ended_at: datetime
    latency_seconds: float


# ---------------------------------------------------------------------------
# Session view
# ---------------------------------------------------------------------------


@dataclass
class SessionView:
    """Read-only-ish view of a Session row passed around between modules."""

    id: str
    user_id: str
    jira_key: str
    primary_project_key: str
    workspace_root: Path
    mode: Literal["brownfield", "greenfield"]
    is_cross_project: bool
    status: Literal["active", "paused", "closed"]
    created_at: datetime
    last_active_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Conversation view
# ---------------------------------------------------------------------------


@dataclass
class ConversationView:
    """Read-only-ish view of a Conversation row + its message buffer."""

    id: str
    session_id: str
    stage: str
    revision: int
    status: Literal["running", "completed", "failed", "escalated"]
    started_at: datetime
    ended_at: datetime | None
    messages: list[Message]
    turn_count: int
    tool_call_count: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    llm_provider: str | None = None
    llm_model: str | None = None
    operation_log_id: int | None = None
