"""ai-coding CLI entry point. See ADR-0027 + ADR-0030."""

from __future__ import annotations

import asyncio
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__

app = typer.Typer(
    name="ai-coding",
    help="AI Coding Agent CLI (v0.2 Lite). Run `ai-coding --help` for command list.",
    add_completion=False,
    no_args_is_help=True,
)

sessions_app = typer.Typer(name="sessions", help="Inspect sessions + conversations.")
skills_app = typer.Typer(name="skills", help="Skill discovery + introspection.")
migrate_app = typer.Typer(name="migrate", help="Database migrations.")

app.add_typer(sessions_app, name="sessions")
app.add_typer(skills_app, name="skills")
app.add_typer(migrate_app, name="migrate")

console = Console()


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"ai-coding-cli {__version__}")


@app.command("show-config")
def show_config(
    reveal_secrets: bool = typer.Option(False, "--reveal-secrets"),
) -> None:
    """Print the resolved configuration (secrets redacted by default)."""
    config = _load_config_or_die()
    data: dict[str, Any] = json.loads(config.model_dump_json())
    if not reveal_secrets:
        _redact_secrets(data)
    console.print_json(data=data)


@app.command("show-tools")
def show_tools() -> None:
    """List registered native tools."""
    registry = _import_native_tools()
    for tool in sorted(registry.all(), key=lambda t: t.name):
        visibility = "agent-visible" if tool.visible_to_agent else "orchestrator-only"
        console.print(
            f"  [bold]{tool.name}[/bold] "
            f"[dim]({tool.side_effects.value} / {visibility})[/dim]"
        )
        console.print(f"    {tool.description}")


@app.command()
def doctor() -> None:
    """Health check: config + storage + LLM endpoint reachability."""
    config = _load_config_or_die()
    console.print("[bold]Configuration[/bold] [green]ok[/green]")
    console.print(f"  workspace_path: {config.workspace_path}")
    console.print(f"  db_path: {config.storage.db_path}")

    async def _check_storage() -> bool:
        from .foundation.storage import StorageEngine

        engine = StorageEngine(config.storage.db_path)
        try:
            await engine.ping()
            return True
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Storage check failed:[/red] {exc}")
            return False
        finally:
            await engine.close()

    storage_ok = asyncio.run(_check_storage())
    if storage_ok:
        console.print("[bold]Storage[/bold] [green]ok[/green]")
    else:
        console.print("[bold]Storage[/bold] [red]unavailable[/red]")
        raise typer.Exit(code=2)

    console.print("[bold]LLM[/bold] (configured)")
    console.print(f"  primary: {config.llm.primary.kind} / {config.llm.primary.model_name}")
    if config.llm.primary.base_url:
        console.print(f"  base_url: {config.llm.primary.base_url}")


@app.command()
def chat(
    jira_key: str = typer.Argument(..., help="Jira ticket key, e.g. PROJ-42"),
    force_status: str | None = typer.Option(
        None,
        "--force-status",
        help="Override the current Jira status (e.g. DESIGN_DRAFTING).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip external writes."),
) -> None:
    """Run one pipeline iteration for a Jira ticket manually."""
    config = _load_config_or_die()

    async def _run() -> None:
        orchestrator = await _build_default_orchestrator(config, dry_run=dry_run)
        await orchestrator.manual_invoke(jira_key, force_status=force_status)

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]chat failed:[/red] {exc}")
        raise typer.Exit(code=1)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (localhost only by policy)."),
    port: int = typer.Option(8080, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Start the local Web Dashboard (foreground)."""
    import uvicorn

    config = _load_config_or_die()
    from .application.operation_log import OperationLogReader
    from .foundation.session import SessionManager
    from .foundation.storage import StorageEngine
    from .web import DashboardDeps, build_dashboard_app

    storage = StorageEngine(config.storage.db_path)
    manager = SessionManager(storage)
    reader = OperationLogReader(storage, config.workspace_path)
    deps = DashboardDeps(
        config=config,
        storage=storage,
        session_manager=manager,
        operation_log_reader=reader,
    )
    fastapi_app = build_dashboard_app(deps)
    url = f"http://{host}:{port}/"
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - never block the dashboard on a browser open failure
            pass
    console.print(f"Dashboard at [bold]{url}[/bold] (press Ctrl-C to stop)")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command()
def daemon(
    cmd: str = typer.Argument(..., help="start|status"),
) -> None:
    """Daemon control. Lite supports only `start` (foreground)."""
    config = _load_config_or_die()
    if cmd == "status":
        console.print("[yellow]Lite has no persistent daemon; `start` runs in the foreground.[/yellow]")
        raise typer.Exit(code=0)
    if cmd != "start":
        console.print(f"[red]Unknown daemon subcommand:[/red] {cmd}")
        raise typer.Exit(code=2)

    async def _run() -> None:
        from .application.jira_reaction import JiraReactor, JiraReactorConfig

        orchestrator = await _build_default_orchestrator(config, dry_run=False)
        reactor = JiraReactor(
            orchestrator=orchestrator,
            tool_registry=_import_native_tools(),
            config=config,
            reactor_config=JiraReactorConfig(
                poll_active_seconds=config.jira.poll_active_seconds,
                poll_idle_seconds=config.jira.poll_idle_seconds,
            ),
        )
        console.print("Daemon (Lite) running. Polling Jira; Ctrl-C to stop.")
        try:
            await reactor.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("Stopping reactor...")
            reactor.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# `migrate` subcommands
# ---------------------------------------------------------------------------


@migrate_app.command("up")
def migrate_up() -> None:
    """Apply pending Alembic migrations to the configured SQLite database."""
    config = _load_config_or_die()
    _ensure_db_path(config.storage.db_path)
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig
    except ImportError as exc:
        console.print(f"[red]Alembic not installed:[/red] {exc}")
        raise typer.Exit(code=1)

    alembic_ini = Path(__file__).parent.parent.parent / "migrations" / "sqlite" / "alembic.ini"
    if not alembic_ini.is_file():
        # Fallback: use packaged path if installed via wheel.
        alembic_ini = Path(__file__).parent / "_migrations" / "alembic.ini"
    if not alembic_ini.is_file():
        console.print(f"[red]alembic.ini not found at {alembic_ini}.[/red]")
        raise typer.Exit(code=1)

    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{config.storage.db_path}")
    command.upgrade(cfg, "head")
    console.print(f"[green]Migrations applied to {config.storage.db_path}[/green]")


# ---------------------------------------------------------------------------
# `sessions` subcommands
# ---------------------------------------------------------------------------


@sessions_app.command("list")
def sessions_list(limit: int = typer.Option(20, "--limit")) -> None:
    """List recent sessions."""
    config = _load_config_or_die()

    async def _run() -> list[dict[str, Any]]:
        from sqlalchemy import desc, select
        from sqlalchemy.exc import OperationalError

        from .foundation.storage import Session, StorageEngine

        engine = StorageEngine(config.storage.db_path)
        try:
            async with engine.session() as s:
                rows = (
                    await s.execute(
                        select(Session).order_by(desc(Session.last_active_at)).limit(limit)
                    )
                ).scalars().all()
            return [
                {
                    "id": r.id[:8],
                    "jira_key": r.jira_key,
                    "user": r.user_id,
                    "mode": r.mode,
                    "status": r.status,
                    "last_active": r.last_active_at.isoformat(timespec="seconds"),
                }
                for r in rows
            ]
        except OperationalError as exc:
            if "no such table" in str(exc).lower():
                console.print(
                    "[yellow]Database not initialized. "
                    "Run [bold]ai-coding migrate up[/bold] first.[/yellow]"
                )
                raise typer.Exit(code=2)
            raise
        finally:
            await engine.close()

    rows = asyncio.run(_run())
    if not rows:
        console.print("[dim]No sessions yet.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("id", "jira_key", "user", "mode", "status", "last_active"):
        table.add_column(col)
    for r in rows:
        table.add_row(*[str(r[c]) for c in ("id", "jira_key", "user", "mode", "status", "last_active")])
    console.print(table)


# ---------------------------------------------------------------------------
# `skills` subcommands
# ---------------------------------------------------------------------------


@skills_app.command("list")
def skills_list() -> None:
    """List discovered skills in precedence order."""
    config = _load_config_or_die()
    from .foundation.skills import SkillLoader

    loader = SkillLoader(workspace_root=config.workspace_path)
    entries = loader.scan()
    if not entries:
        console.print("[dim]No skills discovered.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("source")
    table.add_column("version")
    table.add_column("scope")
    table.add_column("tokens (est.)")
    table.add_column("description")
    for e in entries:
        table.add_row(
            e.name,
            e.source_level,
            e.version,
            e.scope,
            str(e.body_token_estimate),
            e.description,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config_or_die():  # type: ignore[no-untyped-def]
    """Load Config; exit non-zero on failure with a clear message."""
    try:
        from .foundation.config import load_config

        return load_config()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(code=2)


def _import_native_tools():  # type: ignore[no-untyped-def]
    from .foundation.tools import global_registry
    from .foundation.tools.native import (  # noqa: F401
        git_tools,
        github_tools,
        jira_tools,
        repo_tools,
        test_tools,
    )

    return global_registry()


async def _build_default_orchestrator(config, *, dry_run: bool):  # type: ignore[no-untyped-def]
    """Wire up the default PipelineOrchestrator with DesignStageHandler registered."""
    from .application.operation_log import (
        OperationLogReader,
        OperationLogWriter,
    )
    from .application.pipeline import PipelineOrchestrator, PipelineStateMachine
    from .application.pipeline.stages.design import DesignStageHandler
    from .foundation.compactor import Compactor
    from .foundation.context import ContextBuilder
    from .foundation.llm import build_adapter
    from .foundation.session import SessionManager
    from .foundation.storage import StorageEngine

    registry = _import_native_tools()

    _ensure_db_path(config.storage.db_path)
    storage = StorageEngine(config.storage.db_path)
    manager = SessionManager(storage)
    writer = OperationLogWriter(storage, config.workspace_path)
    reader = OperationLogReader(storage, config.workspace_path)

    state_machine = PipelineStateMachine()
    state_machine.register(DesignStageHandler())

    adapter = build_adapter(config.llm.primary)
    compactor = Compactor(adapter)
    builder = ContextBuilder()

    return PipelineOrchestrator(
        state_machine=state_machine,
        storage=storage,
        session_manager=manager,
        operation_log_writer=writer,
        operation_log_reader=reader,
        tool_registry=registry,
        llm=adapter,
        compactor=compactor,
        context_builder=builder,
        config=config,
        primary_project_key=config.jira.base_url.host.split(".")[0] if config.jira.base_url else "UNKNOWN",
        dry_run=dry_run,
    )


def _ensure_db_path(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _redact_secrets(node: Any) -> None:
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
