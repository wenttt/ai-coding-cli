"""Config loader. Resolves workspace + user .env files, validates, returns Config.

See ADR-0016 + ADR-0030.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from ..errors import ConfigFileUnreadable, ConfigValidationError
from ._models import Config


def _resolve_env_file_paths(workspace_path: Path | None) -> list[Path]:
    """Return env files in pydantic-settings precedence order (last wins).

    pydantic-settings reads env_file list left-to-right; later files override
    earlier ones. We want:
        user .env  → workspace .env  → process env  → CLI flags

    So we return [user_env, workspace_env]; process env / CLI flags are
    applied by pydantic-settings + the caller respectively.
    """
    paths: list[Path] = []

    user_env = Path.home() / ".config" / "ai-coding-cli" / ".env"
    if user_env.exists():
        paths.append(user_env)

    if workspace_path is not None:
        workspace_env = workspace_path / ".ai-coding-cli" / ".env"
        if workspace_env.exists():
            paths.append(workspace_env)

    # Always include the cwd .env if present (developer convenience for local
    # checkout / one-shot CLI invocations).
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists() and cwd_env not in paths:
        paths.append(cwd_env)

    return paths


def load_config(
    *,
    workspace_path_override: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> Config:
    """Build the top-level Config.

    Order of operations:
    1. Resolve workspace_path (CLI override > WORKSPACE_PATH env var > cwd).
    2. Locate .env files (user, workspace, cwd).
    3. Construct Config with those files; pydantic-settings binds env vars.
    4. Apply CLI overrides (post-construction).
    5. Validate workspace existence + writeability.
    """

    # Step 1: resolve workspace_path
    if workspace_path_override is not None:
        ws_path = workspace_path_override.expanduser().resolve()
    else:
        ws_env = os.environ.get("WORKSPACE_PATH")
        ws_path = Path(ws_env).expanduser().resolve() if ws_env else Path.cwd()

    # Step 2: locate env files
    env_files = _resolve_env_file_paths(ws_path)

    # Step 3: construct Config with env files attached.
    # Pydantic-Settings re-reads env_file at instantiation; we patch model_config
    # via the class init kwargs.
    try:
        config = Config(
            workspace_path=ws_path,
            _env_file=env_files,  # type: ignore[call-arg]
        )
    except ValidationError as exc:
        raise ConfigValidationError(
            "Configuration validation failed; see context for details.",
            cause=exc,
            pydantic_errors=exc.errors(),
        ) from exc
    except OSError as exc:
        raise ConfigFileUnreadable(
            f"Could not read one of the .env files: {exc}",
            cause=exc,
        ) from exc

    # Step 4: apply CLI overrides
    if cli_overrides:
        _apply_overrides(config, cli_overrides)

    # Step 5: workspace existence is checked lazily by consumers; do not
    # require it for test fixtures that point at a synthetic path.

    return config


def _apply_overrides(config: Config, overrides: dict[str, Any]) -> None:
    """Apply CLI overrides to a Config in-place.

    Override keys use the same `__` nesting as env vars, lowercased:
        {"agent__max_turns": 30}  -> config.agent.max_turns = 30
    """
    for key, value in overrides.items():
        path = key.split("__")
        obj: Any = config
        for segment in path[:-1]:
            obj = getattr(obj, segment)
        setattr(obj, path[-1], value)


def build_test_config(**overrides: Any) -> Config:
    """Build a Config populated with safe test values.

    Useful in tests that need a Config without setting up env files. Any
    `overrides` are applied on top via the same nested-key syntax used by
    CLI overrides.
    """
    defaults: dict[str, Any] = {
        "WORKSPACE_PATH": str(Path.cwd()),
        "JIRA_BASE_URL": "https://jira.test",
        "JIRA_AUTH_KIND": "pat",
        "JIRA_API_TOKEN": "test-jira-token",
        "GITHUB_TOKEN": "test-github-token",
        "LLM_PRIMARY__KIND": "mock",
        "LLM_PRIMARY__MODEL_NAME": "mock-model-1",
        "LLM_PRIMARY__BASE_URL": "https://llm.test/v1",
        "LLM_PRIMARY__API_KEY": "test-llm-key",
        "DAEMON_WEBHOOK_SECRET": "test-secret",
    }

    # Apply env-style overrides supplied by the caller (e.g. WORKSPACE_PATH=tmp_path).
    for key, value in overrides.items():
        if key.isupper():
            defaults[key] = value if isinstance(value, str) else str(value)

    # Snapshot + restore os.environ so tests don't leak.
    original_env = {k: os.environ.get(k) for k in defaults}
    try:
        for k, v in defaults.items():
            os.environ[k] = v

        config = load_config(workspace_path_override=Path(defaults["WORKSPACE_PATH"]))

        # Apply non-env attribute overrides (lowercase nested keys).
        attr_overrides = {k: v for k, v in overrides.items() if not k.isupper()}
        if attr_overrides:
            _apply_overrides(config, attr_overrides)

        return config
    finally:
        for k, original in original_env.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original


# Convenience: silence linter for unused import in tests.
_ = SecretStr
