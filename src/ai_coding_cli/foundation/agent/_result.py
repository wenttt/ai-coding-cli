"""AgentResult dataclass. See ADR-0009."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..errors import AgentError


class AgentOutcome(StrEnum):
    """Terminal state reached when Agent.run() returns. See ADR-0009."""

    COMPLETED = "completed"
    MAX_TURNS_HIT = "max_turns_hit"
    MAX_TOKENS_HIT = "max_tokens_hit"
    FATAL_ERROR = "fatal_error"
    USER_ABORT = "user_abort"


@dataclass(frozen=True)
class AgentResult:
    """One Agent.run() result. Consumed by StageHandlers."""

    outcome: AgentOutcome
    final_assistant_message: str | None
    turns_used: int
    tool_calls_made: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cache_hit_tokens: int
    error: AgentError | None = None
    compaction_passes: int = 0
    compaction_tokens_saved: int = 0
    notes: list[str] = field(default_factory=list)
