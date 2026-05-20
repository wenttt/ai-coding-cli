"""Skill types. See ADR-0012."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


# Source levels in precedence order (highest first).
SourceLevel = Literal[
    "workspace",
    "workspace_claude",
    "user",
    "user_claude",
    "builtin",
]


class SkillFrontmatter(BaseModel):
    """YAML frontmatter atop every SKILL.md file. ADR-0012 §File format."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: str = "0.0.0"
    scope: str = "manual"
    tools_allowed: list[str] = Field(default_factory=list)
    tools_required: list[str] = Field(default_factory=list)
    max_skill_tokens: int = 8_000
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class SkillIndexEntry:
    """One scanned skill — appears in `SkillLoader.scan()`'s result."""

    name: str
    description: str
    version: str
    scope: str
    source_level: SourceLevel
    file_path: Path
    body_token_estimate: int
    tools_required: list[str] = field(default_factory=list)
    tools_allowed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadedSkill:
    """Output of SkillLoader.load(name). The body is the rendered Markdown
    (frontmatter stripped) ready for ContextBuilder injection.
    """

    name: str
    version: str
    body: str
    source_level: SourceLevel
    body_token_estimate: int
    tools_required: list[str] = field(default_factory=list)
    tools_allowed: list[str] = field(default_factory=list)
