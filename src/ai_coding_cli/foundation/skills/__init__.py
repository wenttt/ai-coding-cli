"""Skill Loader. See ADR-0012.

Public exports:
    - SkillLoader: discovery + index + preload selection
    - SkillIndexEntry, LoadedSkill, ScopeContext
    - parse_skill_file: standalone parser
"""

from __future__ import annotations

from ._loader import SkillLoader
from ._scope import ScopeContext, evaluate_scope
from ._types import SkillIndexEntry, SkillFrontmatter, LoadedSkill
from ._parser import parse_skill_file

__all__ = [
    "SkillLoader",
    "SkillIndexEntry",
    "SkillFrontmatter",
    "LoadedSkill",
    "ScopeContext",
    "evaluate_scope",
    "parse_skill_file",
]
