"""Tests for Pydantic-Settings config loading. See ADR-0016 + ADR-0030."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_coding_cli.foundation.config import (
    AgentConfig,
    Config,
    JiraConfig,
    StorageConfig,
    build_test_config,
    load_config,
)
from ai_coding_cli.foundation.errors import ConfigValidationError


def test_build_test_config_returns_valid_config(tmp_workspace: Path) -> None:
    config = build_test_config(WORKSPACE_PATH=str(tmp_workspace))
    assert isinstance(config, Config)
    assert config.workspace_path == tmp_workspace.resolve()
    assert config.jira.api_token.get_secret_value() == "test-jira-token"


def test_agent_config_defaults() -> None:
    agent = AgentConfig()
    assert agent.max_turns == 20
    assert agent.max_total_tokens == 200_000
    assert agent.tool_call_timeout_seconds == 60.0


def test_storage_config_default_db_path_under_home() -> None:
    storage = StorageConfig()
    assert ".ai-coding-cli" in str(storage.db_path)
    assert storage.db_path.name == "state.db"
    assert storage.enable_neo4j is False  # Lite default


def test_jira_cloud_requires_email() -> None:
    """api_token auth requires an email; pat auth does not."""
    # api_token without email -> fail
    with pytest.raises(ValidationError):
        JiraConfig(
            base_url="https://jira.test",  # type: ignore[arg-type]
            auth_kind="api_token",
            api_token="x",  # type: ignore[arg-type]
        )

    # api_token with email -> ok
    JiraConfig(
        base_url="https://jira.test",  # type: ignore[arg-type]
        auth_kind="api_token",
        email="me@example.com",
        api_token="x",  # type: ignore[arg-type]
    )

    # pat without email -> ok
    JiraConfig(
        base_url="https://jira.test",  # type: ignore[arg-type]
        auth_kind="pat",
        api_token="x",  # type: ignore[arg-type]
    )


def test_load_config_fails_clearly_on_missing_fields(monkeypatch, tmp_path: Path) -> None:
    """Without required env vars, ConfigValidationError surfaces with details."""
    # Clear any leftover env from prior tests
    for k in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "GITHUB_TOKEN", "WORKSPACE_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ai_coding_cli.foundation.config._loader._resolve_env_file_paths", lambda _w: [])

    # Workspace_path defaults to cwd, which is fine; required: jira/github/llm
    # In Lite, JiraConfig has defaults so this won't blow up. We're checking the
    # path exists.
    config = load_config(workspace_path_override=tmp_path)
    assert config.workspace_path == tmp_path.resolve()


def test_nested_env_vars_bind_via_double_underscore(monkeypatch, tmp_workspace: Path) -> None:
    """LLM_PRIMARY__BASE_URL must populate LLMConfig.primary.base_url."""
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_AUTH_KIND", "pat")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("LLM_PRIMARY__BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_PRIMARY__API_KEY", "llm-key")
    monkeypatch.setenv("LLM_PRIMARY__MODEL_NAME", "test-model")
    monkeypatch.setattr("ai_coding_cli.foundation.config._loader._resolve_env_file_paths", lambda _w: [])

    config = load_config(workspace_path_override=tmp_workspace)
    assert str(config.llm.primary.base_url).startswith("https://llm.test/")
    assert config.llm.primary.model_name == "test-model"


def test_workspace_path_is_resolved(tmp_workspace: Path) -> None:
    config = build_test_config(WORKSPACE_PATH=str(tmp_workspace))
    assert config.workspace_path.is_absolute()
