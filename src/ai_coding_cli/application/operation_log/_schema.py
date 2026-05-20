"""Operation log Pydantic schemas. See ADR-0005."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Body (5 required sections)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationLogBody:
    """The 5 required body sections. Empty values are rejected at write time."""

    what_was_done: str
    impact: str
    what_i_could_not_do: str
    engineering_decisions: str
    next_step: str

    def validate_nonempty(self) -> None:
        """Raise ValueError if any section is whitespace-only."""
        for name in (
            "what_was_done",
            "impact",
            "what_i_could_not_do",
            "engineering_decisions",
            "next_step",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(
                    f"Operation log section {name!r} is empty; sections must be "
                    f"non-empty (use '_(none)_' as an explicit placeholder if needed)."
                )

    def to_markdown(self) -> str:
        sections = [
            ("What was done", self.what_was_done),
            ("Impact", self.impact),
            ("What I could not do", self.what_i_could_not_do),
            ("Engineering decisions", self.engineering_decisions),
            ("Next step", self.next_step),
        ]
        return "\n\n".join(
            f"## {title}\n\n{content.strip()}" for title, content in sections
        )


# ---------------------------------------------------------------------------
# Retry context
# ---------------------------------------------------------------------------


class RetryContext(BaseModel):
    """Populated when revision > 1. See ADR-0005."""

    previous_attempts: list[str] = Field(
        default_factory=list,
        description="1-line summaries of prior attempts, oldest first.",
    )
    failure_signal: str | None = None


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


# Canonical stage slugs the orchestrator + dashboard understand.
STAGE_SLUGS = (
    "design",
    "design-rework",
    "implement",
    "self-review",
    "test-write",
    "test-run",
    "pr-review-fix",
    "deploy",
    "doc-update",
    "investigate",
)

AGENT_KINDS = ("claude-code", "copilot", "cursor", "direct")


class OperationLogFrontmatter(BaseModel):
    """The YAML frontmatter at the top of every operation log Markdown file."""

    jira_key: str
    stage: str
    revision: int = Field(ge=1)
    status: Literal["completed", "failed", "escalated"]
    skill_invoked: str | None = None
    agent: str
    timestamp: datetime
    duration_seconds: float = Field(ge=0)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    retry_context: RetryContext | None = None
    escalation_reason: str | None = None

    @model_validator(mode="after")
    def _check_escalated_consistency(self) -> "OperationLogFrontmatter":
        if self.status == "escalated" and self.escalation_reason is None:
            raise ValueError("escalation_reason required when status='escalated'")
        if self.stage not in STAGE_SLUGS:
            raise ValueError(
                f"Unknown stage slug {self.stage!r}; must be one of {STAGE_SLUGS}"
            )
        if self.agent not in AGENT_KINDS:
            raise ValueError(
                f"Unknown agent kind {self.agent!r}; must be one of {AGENT_KINDS}"
            )
        return self


# ---------------------------------------------------------------------------
# Writer return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WrittenOperationLog:
    """Returned by OperationLogWriter.write()."""

    file_path: Path
    relative_path: str
    sequence_number: int
    revision: int
    sha256: str
    db_row_id: int
