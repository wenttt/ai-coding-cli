"""SkillLoader: discover + index + load Skills. See ADR-0012."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from ..tools import ToolRegistry
from ._parser import parse_skill_file
from ._scope import ScopeContext, evaluate_scope
from ._types import LoadedSkill, SkillIndexEntry, SourceLevel

logger = logging.getLogger(__name__)


# Precedence-ordered source directory templates. Each tuple is
# (source_level, path_template, claude_compat).
_SOURCE_TEMPLATES = (
    ("workspace", "{workspace}/.ai-coding-cli/skills"),
    ("workspace_claude", "{workspace}/.claude/skills"),
    ("user", "{home}/.config/ai-coding-cli/skills"),
    ("user_claude", "{home}/.claude/skills"),
)


class SkillLoader:
    """Discovers + loads Skills from workspace / user / Claude paths + builtin."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        user_home: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        builtin_root: Path | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._user_home = user_home or Path.home()
        self._tool_registry = tool_registry
        self._builtin_root = builtin_root or _default_builtin_root()
        self._index: dict[str, SkillIndexEntry] = {}

    # -----------------------------------------------------------------
    # Discovery + indexing
    # -----------------------------------------------------------------

    def scan(self) -> list[SkillIndexEntry]:
        """Scan all source levels (in precedence order) + builtin. Higher-
        precedence sources WIN if the same name appears at multiple levels.

        Mutates the internal index. Returns the list of indexed entries.
        """
        self._index.clear()
        for source_level, template in _SOURCE_TEMPLATES:
            root = Path(
                template.format(
                    workspace=self._workspace_root,
                    home=self._user_home,
                )
            )
            self._scan_dir(root, source_level)  # type: ignore[arg-type]
        self._scan_dir(self._builtin_root, "builtin")
        return list(self._index.values())

    @property
    def index(self) -> dict[str, SkillIndexEntry]:
        return dict(self._index)

    def _scan_dir(self, root: Path, source_level: SourceLevel) -> None:
        if not root.is_dir():
            return
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            entry = self._parse_to_entry(skill_file, source_level)
            if entry is None:
                continue
            # Higher precedence wins: only insert if not already present.
            if entry.name in self._index:
                logger.debug(
                    "skills.precedence_skip name=%s lower_level=%s higher_level=%s",
                    entry.name,
                    source_level,
                    self._index[entry.name].source_level,
                )
                continue
            self._index[entry.name] = entry

    def _parse_to_entry(
        self, file_path: Path, source_level: SourceLevel
    ) -> SkillIndexEntry | None:
        parsed = parse_skill_file(file_path)
        if parsed is None:
            return None
        fm = parsed.frontmatter
        body_tokens = max(1, len(parsed.body) // 4)
        return SkillIndexEntry(
            name=fm.name,
            description=fm.description,
            version=fm.version,
            scope=fm.scope,
            source_level=source_level,
            file_path=file_path,
            body_token_estimate=body_tokens,
            tools_required=list(fm.tools_required),
            tools_allowed=list(fm.tools_allowed),
        )

    # -----------------------------------------------------------------
    # Preload selection
    # -----------------------------------------------------------------

    def select_for_preload(self, ctx: ScopeContext) -> list[SkillIndexEntry]:
        """Return skills whose scope expression matches the context."""
        out: list[SkillIndexEntry] = []
        for entry in self._index.values():
            if evaluate_scope(entry.scope, ctx):
                out.append(entry)
        return out

    # -----------------------------------------------------------------
    # Loading
    # -----------------------------------------------------------------

    def load(self, name: str) -> LoadedSkill | None:
        """Load one skill's body. Returns None if not in index or unparseable.

        Verifies `tools_required` are present in the ToolRegistry (when one is
        configured). On failure, logs + returns None.
        """
        entry = self._index.get(name)
        if entry is None:
            logger.warning("skills.unknown name=%s", name)
            return None

        if self._tool_registry is not None and entry.tools_required:
            missing = [
                tname
                for tname in entry.tools_required
                if not self._tool_registry.has(tname)
            ]
            if missing:
                logger.warning(
                    "skills.tools_required_missing name=%s missing=%s",
                    name,
                    missing,
                )
                return None

        parsed = parse_skill_file(entry.file_path)
        if parsed is None:
            return None

        return LoadedSkill(
            name=entry.name,
            version=entry.version,
            body=parsed.body,
            source_level=entry.source_level,
            body_token_estimate=entry.body_token_estimate,
            tools_required=list(entry.tools_required),
            tools_allowed=list(entry.tools_allowed),
        )

    def load_many(self, names: Iterable[str]) -> list[LoadedSkill]:
        out: list[LoadedSkill] = []
        for name in names:
            loaded = self.load(name)
            if loaded is not None:
                out.append(loaded)
        return out


def _default_builtin_root() -> Path:
    """Bundled built-in skills live under foundation/skills/_builtin/."""
    return Path(__file__).parent / "_builtin"
