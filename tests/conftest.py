"""Shared pytest fixtures. See ADR-0018.

Fixtures here are auto-discovered by pytest. Keep this file lean; per-module
fixtures live in subdirectory conftest.py files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding_cli.foundation.config import Config, build_test_config


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A throwaway workspace directory with .ai-coding-cli scaffolding."""
    (tmp_path / ".ai-coding-cli").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def test_config(tmp_workspace: Path) -> Config:
    """A valid Config with safe test values."""
    return build_test_config(WORKSPACE_PATH=str(tmp_workspace))
