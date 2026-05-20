"""Configuration (Pydantic-Settings). See ADR-0016 + ADR-0030.

Sources, in precedence order (highest first):
1. CLI flags (applied by Typer outside this module)
2. Process env vars (`AGENT_MAX_TURNS=20`, `LLM_PRIMARY__BASE_URL=...`)
3. Workspace `.env`            ({workspace}/.ai-coding-cli/.env)
4. User `.env`                 (~/.config/ai-coding-cli/.env)
5. Built-in defaults

Nested settings use `__` as the delimiter:
    LLM_PRIMARY__BASE_URL=https://llm.company.com/v1
binds to LLMConfig.primary.base_url.
"""

from __future__ import annotations

from ._models import (
    AdapterConfig,
    AgentConfig,
    Config,
    DaemonConfig,
    GitHubConfig,
    GuardrailConfig,
    JiraConfig,
    LLMConfig,
    LLMEmbeddingConfig,
    ObservabilityConfig,
    SkillConfig,
    StorageConfig,
    WebDashboardConfig,
)
from ._loader import build_test_config, load_config

__all__ = [
    "Config",
    "AdapterConfig",
    "AgentConfig",
    "DaemonConfig",
    "GitHubConfig",
    "GuardrailConfig",
    "JiraConfig",
    "LLMConfig",
    "LLMEmbeddingConfig",
    "ObservabilityConfig",
    "SkillConfig",
    "StorageConfig",
    "WebDashboardConfig",
    "load_config",
    "build_test_config",
]
