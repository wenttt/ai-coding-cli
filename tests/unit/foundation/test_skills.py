"""SkillLoader tests. See ADR-0012."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding_cli.foundation.skills import (
    ScopeContext,
    SkillLoader,
    evaluate_scope,
    parse_skill_file,
)


def _write_skill(
    root: Path,
    name: str,
    *,
    scope: str = "manual",
    body: str = "Skill body content.",
    description: str = "Test skill",
    version: str = "1.0.0",
    tools_required: list[str] | None = None,
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        f"name: {name}",
        f"description: {description}",
        f"version: {version}",
        f"scope: {scope}",
    ]
    if tools_required:
        fm_lines.append("tools_required: [" + ", ".join(tools_required) + "]")
    text = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body + "\n"
    file_path = skill_dir / "SKILL.md"
    file_path.write_text(text, encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_reads_frontmatter_and_body(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "demo", scope="stage:design", body="# Demo")
    parsed = parse_skill_file(path)
    assert parsed is not None
    assert parsed.frontmatter.name == "demo"
    assert parsed.frontmatter.scope == "stage:design"
    assert parsed.body.startswith("# Demo")


def test_parser_rejects_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "bad" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Just a body, no frontmatter.\n", encoding="utf-8")
    assert parse_skill_file(path) is None


def test_parser_backfills_claude_code_defaults(tmp_path: Path) -> None:
    """A Claude Code skill may omit version + scope."""
    path = tmp_path / "claude-skill" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: claude-skill\ndescription: from claude\n---\n\nbody\n",
        encoding="utf-8",
    )
    parsed = parse_skill_file(path)
    assert parsed is not None
    assert parsed.frontmatter.scope == "manual"
    assert parsed.frontmatter.version == "0.0.0"


# ---------------------------------------------------------------------------
# Scope evaluator
# ---------------------------------------------------------------------------


def test_scope_always_and_manual() -> None:
    ctx = ScopeContext(stage="design", mode="brownfield")
    assert evaluate_scope("always", ctx) is True
    assert evaluate_scope("manual", ctx) is False


def test_scope_single_atom_match() -> None:
    ctx = ScopeContext(stage="design", mode="brownfield")
    assert evaluate_scope("stage:design", ctx) is True
    assert evaluate_scope("stage:implement", ctx) is False


def test_scope_and_or_expressions() -> None:
    ctx = ScopeContext(stage="implement", mode="brownfield", language="python")
    assert evaluate_scope("stage:implement + language:python", ctx) is True
    assert evaluate_scope("stage:implement + language:go", ctx) is False
    assert evaluate_scope("stage:design, stage:implement", ctx) is True


def test_scope_label_membership() -> None:
    ctx = ScopeContext(jira_labels=("critical", "frontend"))
    assert evaluate_scope("label:critical", ctx) is True
    assert evaluate_scope("label:nope", ctx) is False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_discovers_workspace_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / ".ai-coding-cli" / "skills"
    _write_skill(skills_root, "ws-skill", scope="always")
    loader = SkillLoader(workspace_root=tmp_path, user_home=tmp_path / "home")
    entries = loader.scan()
    names = [e.name for e in entries]
    assert "ws-skill" in names
    entry = next(e for e in entries if e.name == "ws-skill")
    assert entry.source_level == "workspace"


def test_loader_precedence_workspace_wins_over_user(tmp_path: Path) -> None:
    workspace_root = tmp_path / "ws"
    user_home = tmp_path / "home"
    ws_skills = workspace_root / ".ai-coding-cli" / "skills"
    user_skills = user_home / ".config" / "ai-coding-cli" / "skills"
    _write_skill(ws_skills, "dual", description="from workspace")
    _write_skill(user_skills, "dual", description="from user")

    loader = SkillLoader(workspace_root=workspace_root, user_home=user_home)
    loader.scan()
    entry = loader.index["dual"]
    assert entry.source_level == "workspace"
    assert entry.description == "from workspace"


def test_loader_select_for_preload(tmp_path: Path) -> None:
    ws_skills = tmp_path / ".ai-coding-cli" / "skills"
    _write_skill(ws_skills, "design-skill", scope="stage:design")
    _write_skill(ws_skills, "implement-skill", scope="stage:implement")
    _write_skill(ws_skills, "always-skill", scope="always")

    loader = SkillLoader(workspace_root=tmp_path, user_home=tmp_path / "home")
    loader.scan()

    selected = loader.select_for_preload(ScopeContext(stage="design"))
    names = {e.name for e in selected}
    assert "design-skill" in names
    assert "always-skill" in names
    assert "implement-skill" not in names


def test_loader_load_returns_body(tmp_path: Path) -> None:
    ws_skills = tmp_path / ".ai-coding-cli" / "skills"
    _write_skill(ws_skills, "load-me", body="# Custom body\n\nProcedure...")
    loader = SkillLoader(workspace_root=tmp_path, user_home=tmp_path / "home")
    loader.scan()
    loaded = loader.load("load-me")
    assert loaded is not None
    assert loaded.name == "load-me"
    assert loaded.body.startswith("# Custom body")


def test_loader_load_returns_none_for_unknown(tmp_path: Path) -> None:
    loader = SkillLoader(workspace_root=tmp_path, user_home=tmp_path / "home")
    loader.scan()
    assert loader.load("does-not-exist") is None


def test_loader_load_rejects_missing_tools_required(tmp_path: Path) -> None:
    from ai_coding_cli.foundation.tools import ToolRegistry

    ws_skills = tmp_path / ".ai-coding-cli" / "skills"
    _write_skill(
        ws_skills,
        "needs-tools",
        tools_required=["nonexistent_tool"],
    )
    loader = SkillLoader(
        workspace_root=tmp_path,
        user_home=tmp_path / "home",
        tool_registry=ToolRegistry(),
    )
    loader.scan()
    assert loader.load("needs-tools") is None
