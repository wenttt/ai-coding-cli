"""Native repo / filesystem tools. See ADR-0013 §Repo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .._context import ToolContext
from .._decorator import tool
from .._side_effects import SideEffectClass

_CODE_EXTENSIONS: set[str] = {
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".go", ".rs", ".java", ".kt",
    ".rb", ".php",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs", ".swift", ".m",
}

_SCAFFOLDING_NAMES: set[str] = {
    "README.md", "README", "README.rst",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    ".gitignore", ".gitattributes",
    "CHANGELOG.md", "CHANGELOG",
    "CONTRIBUTING.md", "AUTHORS", "MAINTAINERS",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    ".env.example", ".env.template",
}

_GREENFIELD_FILE_THRESHOLD = 5


def _enforce_workspace(workspace: Path, relative_path: str) -> Path:
    """Resolve `workspace / relative_path` and refuse paths outside workspace."""
    target = (workspace / relative_path).resolve()
    workspace_resolved = workspace.resolve()
    try:
        target.relative_to(workspace_resolved)
    except ValueError as exc:
        raise ValueError(
            f"Path {relative_path!r} resolves outside workspace_path; refused."
        ) from exc
    return target


# ---------------------------------------------------------------------------
# read_repo_file
# ---------------------------------------------------------------------------


class ReadRepoFileArgs(BaseModel):
    path: str = Field(description="Path relative to workspace_root.")
    max_bytes: int = Field(default=200_000, ge=1, le=2_000_000)


@tool(
    name="read_repo_file",
    description="Read a file from the workspace. Caps at max_bytes.",
    side_effects=SideEffectClass.READ_ONLY,
)
def read_repo_file(args: ReadRepoFileArgs, ctx: ToolContext) -> str:
    target = _enforce_workspace(ctx.config.workspace_path, args.path)
    if not target.exists():
        raise FileNotFoundError(f"File not found in workspace: {args.path}")
    if not target.is_file():
        raise IsADirectoryError(f"Not a file: {args.path}")
    size = target.stat().st_size
    if size > args.max_bytes:
        return f"<file too large: {size} bytes; use list_repo_files or read in chunks>"
    return target.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# list_repo_files
# ---------------------------------------------------------------------------


class ListRepoFilesArgs(BaseModel):
    directory: str = Field(default="", description="Subdirectory under workspace.")
    glob: str = Field(default="**/*")
    include_hidden: bool = False
    limit: int = Field(default=500, ge=1, le=5000)


@tool(
    name="list_repo_files",
    description="List files under a workspace directory matching a glob pattern. Returns paths relative to workspace root.",
    side_effects=SideEffectClass.READ_ONLY,
)
def list_repo_files(args: ListRepoFilesArgs, ctx: ToolContext) -> list[str]:
    root = _enforce_workspace(ctx.config.workspace_path, args.directory) if args.directory else ctx.config.workspace_path
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for path in root.glob(args.glob):
        if not path.is_file():
            continue
        rel = path.relative_to(ctx.config.workspace_path)
        if not args.include_hidden and any(part.startswith(".") for part in rel.parts):
            continue
        out.append(str(rel))
        if len(out) >= args.limit:
            break
    return out


# ---------------------------------------------------------------------------
# write_repo_file
# ---------------------------------------------------------------------------


class WriteRepoFileArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True


@tool(
    name="write_repo_file",
    description="Write a file (overwriting if exists). Restricted to the workspace.",
    side_effects=SideEffectClass.LOCAL_WRITE,
)
def write_repo_file(args: WriteRepoFileArgs, ctx: ToolContext) -> dict[str, Any]:
    target = _enforce_workspace(ctx.config.workspace_path, args.path)
    if ctx.dry_run:
        return {
            "dry_run": True,
            "would_write_bytes": len(args.content.encode("utf-8")),
            "to": args.path,
        }
    if args.create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args.content, encoding="utf-8")
    return {"path": args.path, "size_bytes": target.stat().st_size}


# ---------------------------------------------------------------------------
# analyze_repo_state
# ---------------------------------------------------------------------------


class AnalyzeRepoStateArgs(BaseModel):
    pass


@tool(
    name="analyze_repo_state",
    description="Detect whether the workspace is brownfield or greenfield. Returns mode, code file count, language distribution, has_tests, has_ci.",
    side_effects=SideEffectClass.READ_ONLY,
)
def analyze_repo_state(args: AnalyzeRepoStateArgs, ctx: ToolContext) -> dict[str, Any]:
    workspace = ctx.config.workspace_path
    code_count = 0
    languages: dict[str, int] = {}
    has_tests = False
    has_ci = False

    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace)
        parts = rel.parts
        if any(p.startswith(".") and p != ".github" for p in parts):
            continue
        if "node_modules" in parts or "__pycache__" in parts or "venv" in parts or ".venv" in parts:
            continue

        name = path.name
        ext = path.suffix.lower()

        if name in _SCAFFOLDING_NAMES:
            continue
        if ext in _CODE_EXTENSIONS:
            code_count += 1
            languages[ext] = languages.get(ext, 0) + 1

        if "test" in name.lower() or "spec" in name.lower():
            has_tests = True

        if ".github" in parts and "workflows" in parts:
            has_ci = True

    mode = "greenfield" if code_count < _GREENFIELD_FILE_THRESHOLD else "brownfield"
    return {
        "mode": mode,
        "code_file_count": code_count,
        "languages": languages,
        "has_tests": has_tests,
        "has_ci": has_ci,
        "workspace_path": str(workspace),
    }


# ---------------------------------------------------------------------------
# find_relevant_modules
# ---------------------------------------------------------------------------


class FindRelevantModulesArgs(BaseModel):
    keywords: list[str] = Field(min_length=1)
    max_files: int = Field(default=20, ge=1, le=100)


@tool(
    name="find_relevant_modules",
    description="Find code files in the workspace matching any of the given keywords. Returns matches sorted by hit count.",
    side_effects=SideEffectClass.READ_ONLY,
)
def find_relevant_modules(args: FindRelevantModulesArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    workspace = ctx.config.workspace_path
    keywords_lower = [k.lower() for k in args.keywords if k.strip()]
    if not keywords_lower:
        return []

    results: list[dict[str, Any]] = []
    for path in workspace.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _CODE_EXTENSIONS:
            continue
        rel = path.relative_to(workspace)
        if any(p.startswith(".") for p in rel.parts):
            continue
        if "node_modules" in rel.parts or "__pycache__" in rel.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        text_lower = text.lower()
        match_count = sum(text_lower.count(k) for k in keywords_lower)
        if match_count == 0:
            continue

        first_line = ""
        for line in text.splitlines():
            if any(k in line.lower() for k in keywords_lower):
                first_line = line.strip()[:200]
                break

        results.append({
            "path": str(rel),
            "match_count": match_count,
            "first_matching_line": first_line,
        })

    results.sort(key=lambda r: r["match_count"], reverse=True)
    return results[: args.max_files]
