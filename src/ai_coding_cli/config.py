"""Environment-driven configuration.

Loaded once at CLI startup from .env (if present) + process env vars.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # LLM
    openai_base_url: str
    openai_api_key: str
    openai_model: str

    # MCP server
    mcp_server_command: str
    mcp_server_args: list[str]
    mcp_server_env: dict[str, str] = field(default_factory=dict)

    # Agent
    agent_max_turns: int = 20

    # Logging
    log_level: str = "INFO"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            f"See .env.example for the full list of required vars."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _collect_mcp_server_env() -> dict[str, str]:
    """Collect env vars to forward to the MCP server subprocess.

    Convention: any env var starting with `MCP_SERVER_ENV_` is forwarded
    with that prefix stripped. So `MCP_SERVER_ENV_JIRA_EMAIL=foo@bar.com`
    becomes `JIRA_EMAIL=foo@bar.com` in the spawned MCP server's env.

    This keeps all configuration in one .env file (this CLI's .env)
    instead of maintaining two parallel .envs.
    """
    prefix = "MCP_SERVER_ENV_"
    result: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.startswith(prefix):
            result[k[len(prefix):]] = v
    return result


def load_config() -> Config:
    """Read environment + .env into a typed Config."""
    return Config(
        openai_base_url=_required("OPENAI_BASE_URL").rstrip("/"),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_model=_required("OPENAI_MODEL"),
        mcp_server_command=_required("MCP_SERVER_COMMAND"),
        mcp_server_args=shlex.split(_optional("MCP_SERVER_ARGS", "")),
        mcp_server_env=_collect_mcp_server_env(),
        agent_max_turns=int(_optional("AGENT_MAX_TURNS", "20")),
        log_level=_optional("LOG_LEVEL", "INFO"),
    )
