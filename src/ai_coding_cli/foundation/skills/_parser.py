"""SKILL.md parser. YAML frontmatter + Markdown body."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from ._types import SkillFrontmatter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedSkill:
    frontmatter: SkillFrontmatter
    body: str


def parse_skill_file(path: Path) -> ParsedSkill | None:
    """Parse one SKILL.md file. Returns None on parse failure (logs warning).

    Frontmatter is YAML between leading `---` markers. Defaults applied for
    Claude Code-style skills that omit some fields.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("skills.read_failed path=%s: %s", path, exc)
        return None

    if not text.startswith("---\n"):
        logger.warning("skills.no_frontmatter path=%s", path)
        return None

    end = text.find("\n---\n", 4)
    if end == -1:
        logger.warning("skills.unterminated_frontmatter path=%s", path)
        return None

    yaml_block = text[4:end]
    body = text[end + 5 :].lstrip()
    try:
        fm_data = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as exc:
        logger.warning("skills.yaml_error path=%s: %s", path, exc)
        return None

    if not isinstance(fm_data, dict):
        logger.warning("skills.frontmatter_not_dict path=%s", path)
        return None

    # Backfill defaults for Claude Code-style skills (no version / scope).
    fm_data.setdefault("scope", "manual")
    fm_data.setdefault("version", "0.0.0")
    fm_data.setdefault("description", fm_data.get("name", path.parent.name))

    try:
        frontmatter = SkillFrontmatter.model_validate(fm_data)
    except ValidationError as exc:
        logger.warning("skills.frontmatter_invalid path=%s: %s", path, exc)
        return None

    return ParsedSkill(frontmatter=frontmatter, body=body)
