"""ai-coding CLI entry point. See ADR-0027 + ADR-0030.

Week 1 ships a minimal stub: `ai-coding version` + `ai-coding config show`.
Full CLI lands in Week 4 once the daemon + Agent Core are wired up.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="ai-coding",
    help="AI Coding Agent CLI (v0.2 Lite). Run `ai-coding --help` for command list.",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"ai-coding-cli {__version__}")


@app.command("show-config")
def show_config(reveal_secrets: bool = typer.Option(False, "--reveal-secrets")) -> None:
    """Print the resolved configuration (secrets redacted by default)."""
    try:
        from .foundation.config import load_config
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to import config module:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)

    data: dict[str, Any] = json.loads(config.model_dump_json())
    if not reveal_secrets:
        _redact_secrets(data)
    console.print_json(data=data)


@app.command("show-tools")
def show_tools() -> None:
    """List registered native tools."""
    from .foundation.tools import global_registry
    from .foundation.tools.native import jira_tools, github_tools, git_tools, repo_tools, test_tools  # noqa: F401

    registry = global_registry()
    for tool in sorted(registry.all(), key=lambda t: t.name):
        visibility = "agent-visible" if tool.visible_to_agent else "orchestrator-only"
        console.print(
            f"  [bold]{tool.name}[/bold] "
            f"[dim]({tool.side_effects.value} / {visibility})[/dim]"
        )
        console.print(f"    {tool.description}")


def _redact_secrets(node: Any) -> None:
    """Recursively replace fields containing 'token', 'api_key', 'password', 'secret'."""
    if isinstance(node, dict):
        for k, v in list(node.items()):
            lowered = k.lower()
            if any(s in lowered for s in ("token", "api_key", "password", "secret")):
                node[k] = "<redacted>"
            else:
                _redact_secrets(v)
    elif isinstance(node, list):
        for item in node:
            _redact_secrets(item)


if __name__ == "__main__":  # pragma: no cover
    app()
