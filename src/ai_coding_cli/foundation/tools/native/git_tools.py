"""Native git tools. See ADR-0013 §Git.

git is invoked via subprocess against the workspace path. Per ADR-0024,
write-class operations record side-effects so operation logs can surface
them. The Lite profile takes care to pass stdin=DEVNULL + a clean env so
the corporate-Windows quirks (AV scanning subprocess startup) don't make
us hang.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .._context import ToolContext
from .._decorator import tool
from .._side_effects import SideEffectClass


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Run `git <args>` in `cwd`. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {shlex.join(args)} failed (rc={proc.returncode}):\n{stderr}"
        )
    return proc.returncode or 0, stdout, stderr


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


class GitStatusArgs(BaseModel):
    short: bool = Field(default=True)


@tool(
    name="git_status",
    description="git status in the workspace. Returns the output as a string.",
    side_effects=SideEffectClass.READ_ONLY,
)
async def git_status(args: GitStatusArgs, ctx: ToolContext) -> str:
    cmd = ["status", "--short"] if args.short else ["status"]
    _, stdout, _ = await _run_git(cmd, cwd=ctx.config.workspace_path)
    return stdout


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


class GitDiffArgs(BaseModel):
    from_ref: str = Field(default="HEAD")
    to_ref: str | None = None
    path: str | None = None
    stat_only: bool = False


@tool(
    name="git_diff",
    description="Return a git diff in the workspace.",
    side_effects=SideEffectClass.READ_ONLY,
)
async def git_diff(args: GitDiffArgs, ctx: ToolContext) -> str:
    cmd: list[str] = ["diff"]
    if args.stat_only:
        cmd.append("--stat")
    if args.to_ref:
        cmd.append(f"{args.from_ref}..{args.to_ref}")
    else:
        cmd.append(args.from_ref)
    if args.path:
        cmd.extend(["--", args.path])
    _, stdout, _ = await _run_git(cmd, cwd=ctx.config.workspace_path)
    return stdout


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


class GitLogArgs(BaseModel):
    max_count: int = Field(default=20, ge=1, le=200)
    path: str | None = None


@tool(
    name="git_log",
    description="Recent commit log.",
    side_effects=SideEffectClass.READ_ONLY,
)
async def git_log(args: GitLogArgs, ctx: ToolContext) -> str:
    cmd: list[str] = ["log", f"-{args.max_count}", "--oneline", "--decorate"]
    if args.path:
        cmd.extend(["--", args.path])
    _, stdout, _ = await _run_git(cmd, cwd=ctx.config.workspace_path)
    return stdout


# ---------------------------------------------------------------------------
# git_create_branch (write)
# ---------------------------------------------------------------------------


class GitCreateBranchArgs(BaseModel):
    name: str
    from_ref: str = Field(default="main")


@tool(
    name="git_create_branch",
    description="Create + checkout a new branch from from_ref.",
    side_effects=SideEffectClass.LOCAL_WRITE,
)
async def git_create_branch(args: GitCreateBranchArgs, ctx: ToolContext) -> dict[str, Any]:
    if ctx.dry_run:
        return {"dry_run": True, "would_create": args.name, "from": args.from_ref}
    await _run_git(["checkout", "-b", args.name, args.from_ref], cwd=ctx.config.workspace_path)
    return {"branch": args.name, "from": args.from_ref}


# ---------------------------------------------------------------------------
# git_commit + git_add + git_push (write)
# ---------------------------------------------------------------------------


class GitAddArgs(BaseModel):
    paths: list[str]


@tool(
    name="git_add",
    description="Stage files in the workspace.",
    side_effects=SideEffectClass.LOCAL_WRITE,
)
async def git_add(args: GitAddArgs, ctx: ToolContext) -> dict[str, Any]:
    if ctx.dry_run:
        return {"dry_run": True, "would_stage": list(args.paths)}
    await _run_git(["add", "--", *args.paths], cwd=ctx.config.workspace_path)
    return {"staged": list(args.paths)}


class GitCommitArgs(BaseModel):
    message: str
    allow_empty: bool = False


@tool(
    name="git_commit",
    description="Create a commit with the given message. Stages must already be set.",
    side_effects=SideEffectClass.LOCAL_WRITE,
)
async def git_commit(args: GitCommitArgs, ctx: ToolContext) -> dict[str, Any]:
    if ctx.dry_run:
        return {"dry_run": True, "would_commit": args.message}
    cmd = ["commit", "-m", args.message]
    if args.allow_empty:
        cmd.append("--allow-empty")
    await _run_git(cmd, cwd=ctx.config.workspace_path)
    _, sha, _ = await _run_git(["rev-parse", "HEAD"], cwd=ctx.config.workspace_path)
    return {"sha": sha.strip(), "message": args.message}


class GitPushArgs(BaseModel):
    branch: str | None = None
    set_upstream: bool = True


@tool(
    name="git_push",
    description="Push the current (or named) branch to origin.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
async def git_push(args: GitPushArgs, ctx: ToolContext) -> dict[str, Any]:
    if ctx.dry_run:
        return {"dry_run": True, "would_push": args.branch or "<current>"}
    cmd = ["push"]
    if args.set_upstream:
        cmd.append("-u")
    cmd.append("origin")
    if args.branch:
        cmd.append(args.branch)
    else:
        _, current_branch, _ = await _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=ctx.config.workspace_path
        )
        cmd.append(current_branch.strip())
    _, stdout, stderr = await _run_git(cmd, cwd=ctx.config.workspace_path)
    return {"stdout": stdout, "stderr": stderr}


# ---------------------------------------------------------------------------
# git_changed_files
# ---------------------------------------------------------------------------


class GitChangedFilesArgs(BaseModel):
    from_ref: str = Field(default="HEAD")
    to_ref: str | None = None


@tool(
    name="git_changed_files",
    description="List file paths changed between two refs (or vs working tree).",
    side_effects=SideEffectClass.READ_ONLY,
)
async def git_changed_files(args: GitChangedFilesArgs, ctx: ToolContext) -> list[str]:
    cmd = ["diff", "--name-only"]
    if args.to_ref:
        cmd.append(f"{args.from_ref}..{args.to_ref}")
    else:
        cmd.append(args.from_ref)
    _, stdout, _ = await _run_git(cmd, cwd=ctx.config.workspace_path)
    return [line.strip() for line in stdout.splitlines() if line.strip()]
