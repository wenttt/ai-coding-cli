"""CLI entry point — Typer-based.

Single command for the MVP: `ai-coding "<your instruction>"`.

Future subcommands (status, history, replay, etc.) live as siblings.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer
from rich.console import Console

from .agent import Agent
from .config import load_config

app = typer.Typer(
    name="ai-coding",
    help="AI Coding Agent CLI — drive the AI Coding Workflow pipeline from one command.",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()


_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


def _load_system_prompt() -> str:
    if not _DEFAULT_SYSTEM_PROMPT_PATH.exists():
        raise RuntimeError(
            f"System prompt file missing at {_DEFAULT_SYSTEM_PROMPT_PATH}. "
            "Reinstall the package."
        )
    return _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


@app.command()
def chat(
    instruction: str = typer.Argument(
        ...,
        help='Your instruction in natural language, e.g. "start working on KAN-4".',
    ),
    system_prompt_file: Path | None = typer.Option(
        None,
        "--system",
        "-s",
        help="Override the bundled system prompt with a file path.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging."),
) -> None:
    """Run one ReAct loop iteration: send INSTRUCTION to the agent, print the result."""
    try:
        # Load .env if present, then config
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass

        config = load_config()
    except RuntimeError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    log_level = "DEBUG" if verbose else config.log_level
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if system_prompt_file is not None:
        system_prompt = system_prompt_file.read_text(encoding="utf-8")
    else:
        system_prompt = _load_system_prompt()

    agent = Agent(config)
    result = agent.run_sync(system_prompt=system_prompt, user_message=instruction)

    # Summary footer
    console.print()
    console.print(
        f"[dim]turns={result.turns_used}  tool_calls={result.tool_calls_made}  "
        f"hit_limit={result.hit_turn_limit}[/dim]"
    )


@app.command()
def show_prompt() -> None:
    """Print the bundled system prompt (the pipeline rules)."""
    console.print(_load_system_prompt())


@app.command()
def version() -> None:
    """Print the version."""
    from . import __version__

    console.print(f"ai-coding-cli {__version__}")


def main() -> None:
    """Console-scripts entry point."""
    app()


if __name__ == "__main__":
    main()
