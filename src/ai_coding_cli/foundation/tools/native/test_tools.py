"""Native test discovery + execution tools. See ADR-0013 §Tests."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .._context import ToolContext
from .._decorator import tool
from .._side_effects import SideEffectClass


# ---------------------------------------------------------------------------
# discover_test_framework
# ---------------------------------------------------------------------------


class DiscoverTestFrameworkArgs(BaseModel):
    pass


@tool(
    name="discover_test_framework",
    description="Detect the test framework used in this workspace (pytest, jest, vitest, go-test, cargo-test, npm-test, maven-or-gradle, unknown).",
    side_effects=SideEffectClass.READ_ONLY,
)
def discover_test_framework(args: DiscoverTestFrameworkArgs, ctx: ToolContext) -> dict[str, Any]:
    framework = _detect_framework(ctx.config.workspace_path)
    return {
        "framework": framework,
        "supported": framework not in {"unknown", "maven-or-gradle"},
    }


def _detect_framework(workspace: Path) -> str:
    if (workspace / "pyproject.toml").exists() or any(workspace.glob("pytest.ini")):
        if any(workspace.rglob("test_*.py")) or any(workspace.rglob("*_test.py")):
            return "pytest"
    if (workspace / "package.json").exists():
        try:
            pkg = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            test_script = str(scripts.get("test", ""))
            if "jest" in test_script:
                return "jest"
            if "vitest" in test_script:
                return "vitest"
            if "test" in scripts:
                return "npm-test"
        except (OSError, json.JSONDecodeError):
            pass
    if (workspace / "go.mod").exists():
        return "go-test"
    if (workspace / "Cargo.toml").exists():
        return "cargo-test"
    if (workspace / "pom.xml").exists() or (workspace / "build.gradle").exists():
        return "maven-or-gradle"
    return "unknown"


# ---------------------------------------------------------------------------
# discover_test_files
# ---------------------------------------------------------------------------


class DiscoverTestFilesArgs(BaseModel):
    path_filter: str | None = None
    max_files: int = Field(default=200, ge=1, le=2000)


@tool(
    name="discover_test_files",
    description="List test files in the workspace based on the detected framework.",
    side_effects=SideEffectClass.READ_ONLY,
)
def discover_test_files(args: DiscoverTestFilesArgs, ctx: ToolContext) -> list[str]:
    workspace = ctx.config.workspace_path
    framework = _detect_framework(workspace)
    patterns: list[str]
    if framework == "pytest":
        patterns = ["test_*.py", "*_test.py", "tests/**/*.py"]
    elif framework in {"jest", "vitest", "npm-test"}:
        patterns = ["**/*.test.ts", "**/*.test.tsx", "**/*.test.js",
                    "**/*.spec.ts", "**/*.spec.js"]
    elif framework == "go-test":
        patterns = ["**/*_test.go"]
    elif framework == "cargo-test":
        patterns = ["tests/**/*.rs", "src/**/*.rs"]
    else:
        patterns = ["**/*test*", "**/*spec*"]

    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        for p in workspace.glob(pattern):
            if not p.is_file():
                continue
            rel = str(p.relative_to(workspace))
            if rel in seen:
                continue
            if args.path_filter and args.path_filter not in rel:
                continue
            seen.add(rel)
            out.append(rel)
            if len(out) >= args.max_files:
                return out
    return out


# ---------------------------------------------------------------------------
# run_tests
# ---------------------------------------------------------------------------


class RunTestsArgs(BaseModel):
    test_paths: list[str] | None = None
    timeout_seconds: int = Field(default=600, ge=10, le=3600)


@tool(
    name="run_tests",
    description="Run the workspace's tests. Returns counts of passed/failed/skipped + stdout/stderr (truncated).",
    side_effects=SideEffectClass.LOCAL_WRITE,
)
async def run_tests(args: RunTestsArgs, ctx: ToolContext) -> dict[str, Any]:
    workspace = ctx.config.workspace_path
    framework = _detect_framework(workspace)
    cmd = _command_for(framework, args.test_paths or [])
    timed_out = False
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=args.timeout_seconds
        )
        exit_code = proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        stdout_b = b""
        stderr_b = b""
        exit_code = -1
        timed_out = True

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    parsed = _parse_results(framework, stdout, stderr)

    return {
        "framework": framework,
        "command": " ".join(cmd),
        "exit_code": exit_code,
        "timed_out": timed_out,
        **parsed,
        "stdout": stdout[-50_000:],
        "stderr": stderr[-20_000:],
    }


def _command_for(framework: str, test_paths: list[str]) -> list[str]:
    if framework == "pytest":
        return ["pytest", "-x", "--tb=short", *test_paths]
    if framework == "jest":
        return ["npx", "jest", *test_paths]
    if framework == "vitest":
        return ["npx", "vitest", "run", *test_paths]
    if framework == "npm-test":
        return ["npm", "test"]
    if framework == "go-test":
        return ["go", "test", *test_paths] if test_paths else ["go", "test", "./..."]
    if framework == "cargo-test":
        return ["cargo", "test"]
    raise RuntimeError(
        f"Unknown test framework {framework!r}; configure via repository conventions."
    )


_PYTEST_RESULT_RE = re.compile(
    r"(\d+) passed|(\d+) failed|(\d+) error|(\d+) skipped"
)
_JEST_RESULT_RE = re.compile(
    r"Tests:\s+(?:(\d+) failed,\s+)?(?:(\d+) passed,?\s+)?(\d+) total"
)


def _parse_results(framework: str, stdout: str, stderr: str) -> dict[str, int]:
    combined = (stdout or "") + "\n" + (stderr or "")
    passed = failed = skipped = errors = 0
    if framework == "pytest":
        for m in _PYTEST_RESULT_RE.finditer(combined):
            if m.group(1):
                passed += int(m.group(1))
            if m.group(2):
                failed += int(m.group(2))
            if m.group(3):
                errors += int(m.group(3))
            if m.group(4):
                skipped += int(m.group(4))
    elif framework in {"jest", "vitest"}:
        m = _JEST_RESULT_RE.search(combined)
        if m:
            failed = int(m.group(1) or 0)
            passed = int(m.group(2) or 0)
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "total": passed + failed + skipped + errors,
    }
