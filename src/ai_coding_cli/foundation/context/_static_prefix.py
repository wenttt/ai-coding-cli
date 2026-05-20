"""Tier 2 Static Prefix assembler. See ADR-0010."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..session import SessionView

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoFacts:
    """High-level facts about the workspace. Computed once per Session."""

    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    top_level_modules: list[str] = field(default_factory=list)
    has_tests: bool = False
    has_ci: bool = False


@dataclass(frozen=True)
class LoadedSkill:
    """A skill loaded into the Session at Conversation start time."""

    name: str
    content: str


class StaticPrefixAssembler:
    """Assemble the Tier 2 Static Prefix string from session-level inputs.

    The output is one big string that becomes a single `{"role": "system"}`
    message. Bracketed section headers give the LLM stable anchors per
    ADR-0010 ("per [PROJECT CONVENTIONS] ..."). Section order is fixed.
    """

    def assemble(
        self,
        *,
        session: SessionView,
        conventions: str | None,
        repo_facts: RepoFacts,
        loaded_skills: list[LoadedSkill],
        operation_log_path: str | None = None,
    ) -> str:
        sections: list[str] = []

        if conventions:
            sections.append("[PROJECT CONVENTIONS]\n" + conventions.rstrip())

        sections.append(_format_repo_facts(repo_facts))
        sections.append(
            _format_session(session, operation_log_path=operation_log_path)
        )

        if loaded_skills:
            sections.append(_format_loaded_skills(loaded_skills))

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Section formatters
# ---------------------------------------------------------------------------


def _format_repo_facts(repo_facts: RepoFacts) -> str:
    lines = ["[REPO FACTS]"]
    if repo_facts.languages:
        lines.append(f"- Languages: {', '.join(repo_facts.languages)}")
    if repo_facts.frameworks:
        lines.append(f"- Frameworks: {', '.join(repo_facts.frameworks)}")
    if repo_facts.top_level_modules:
        lines.append(f"- Top-level modules: {', '.join(repo_facts.top_level_modules)}")
    lines.append(f"- Has tests: {'yes' if repo_facts.has_tests else 'no'}")
    lines.append(f"- Has CI: {'yes' if repo_facts.has_ci else 'no'}")
    if len(lines) == 1:
        lines.append("- (workspace is new / unknown; agent must explore)")
    return "\n".join(lines)


def _format_session(
    session: SessionView,
    *,
    operation_log_path: str | None,
) -> str:
    lines = [
        "[SESSION]",
        f"- Jira ticket: {session.jira_key}",
        f"- Project: {session.primary_project_key}",
        f"- Mode: {session.mode}",
        f"- Workspace: {session.workspace_root}",
    ]
    if session.is_cross_project:
        lines.append("- Cross-project: yes")
    if operation_log_path:
        lines.append(f"- Operation log: {operation_log_path}")
    return "\n".join(lines)


def _format_loaded_skills(skills: list[LoadedSkill]) -> str:
    blocks = ["[LOADED SKILLS]"]
    for skill in skills:
        body = skill.content.rstrip()
        if not body:
            logger.warning(
                "Skill %r has empty body; omitting from Static Prefix.", skill.name
            )
            continue
        blocks.append(f"[SKILL: {skill.name}]\n{body}\n[/SKILL]")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Convenience: read conventions.md if present
# ---------------------------------------------------------------------------


def read_conventions_file(workspace_root: Path) -> str | None:
    """Best-effort read of `.ai-coding-cli/conventions.md`. Returns None if
    absent or unreadable; logs a warning on unparseable content.
    """
    candidate = workspace_root / ".ai-coding-cli" / "conventions.md"
    if not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", candidate, exc)
        return None
