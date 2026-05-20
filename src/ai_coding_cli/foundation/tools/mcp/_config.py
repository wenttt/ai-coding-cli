"""MCP bridge config models + YAML loader. See ADR-0030 §MCP Bridge in Lite."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .._side_effects import SideEffectClass

logger = logging.getLogger(__name__)

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Tool override
# ---------------------------------------------------------------------------


class MCPToolOverride(BaseModel):
    """Per-tool override in `mcp_bridges.yaml`. See ADR-0030."""

    side_effects: SideEffectClass | None = None
    requires_confirmation: bool | None = None
    visible_to_agent: bool | None = None


# ---------------------------------------------------------------------------
# Bridge config
# ---------------------------------------------------------------------------


class MCPBridgeConfig(BaseModel):
    """One bridge entry. ADR-0030 §Sandboxed environment."""

    name: str = Field(min_length=1)
    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: Path | None = None

    # Sandboxed env: ONLY these vars are forwarded to the subprocess.
    # Values may be literal strings OR `${env:VAR}` placeholders.
    env_whitelist: dict[str, str] = Field(default_factory=dict)

    tools_namespace: str = Field(default="", description="Prefix for registered tool names.")
    auto_start: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)

    # Per-tool overrides keyed by the bridge-side tool name.
    tool_overrides: dict[str, MCPToolOverride] = Field(default_factory=dict)

    # Reconnect tuning (ADR-0030 §Lifecycle).
    reconnect_max_attempts: int = Field(default=5, ge=0)
    reconnect_base_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _validate_namespace(self) -> "MCPBridgeConfig":
        if self.tools_namespace and not re.fullmatch(
            r"[a-z][a-z0-9_-]*", self.tools_namespace
        ):
            raise ValueError(
                f"tools_namespace {self.tools_namespace!r} must be kebab/snake_case"
            )
        return self


# ---------------------------------------------------------------------------
# Top-level YAML wrapper
# ---------------------------------------------------------------------------


class MCPBridgesYAML(BaseModel):
    bridges: list[MCPBridgeConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_names(self) -> "MCPBridgesYAML":
        seen: set[str] = set()
        for b in self.bridges:
            if b.name in seen:
                raise ValueError(f"Duplicate bridge name {b.name!r}")
            seen.add(b.name)
        return self


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_bridges_yaml(path: Path | None) -> MCPBridgesYAML:
    """Load + validate mcp_bridges.yaml. Returns empty config when path is
    None or missing.
    """
    if path is None:
        return MCPBridgesYAML(bridges=[])
    if not path.is_file():
        logger.info("mcp.bridges_yaml_absent path=%s", path)
        return MCPBridgesYAML(bridges=[])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("mcp.bridges_yaml_unreadable path=%s: %s", path, exc)
        return MCPBridgesYAML(bridges=[])
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"mcp_bridges.yaml at {path} must be a mapping; got {type(data).__name__}"
        )
    return MCPBridgesYAML.model_validate(data)


# ---------------------------------------------------------------------------
# ${env:VAR} placeholder resolution
# ---------------------------------------------------------------------------


def resolve_env_placeholders(
    template: dict[str, str],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve `${env:VAR}` placeholders against the supplied env (defaults to
    os.environ). Variables not present in `env` are returned as empty string
    (a warning is logged); literal values pass through unchanged.

    The output dict is the resolved env_whitelist the subprocess will receive.
    """
    source = env if env is not None else dict(os.environ)
    out: dict[str, str] = {}
    for key, value in template.items():
        out[key] = _resolve_one(value, source, declared_key=key)
    return out


def _resolve_one(value: str, source: dict[str, str], *, declared_key: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        var_name = m.group(1)
        resolved = source.get(var_name)
        if resolved is None:
            logger.warning(
                "mcp.env_placeholder_missing key=%s var=%s",
                declared_key,
                var_name,
            )
            return ""
        return resolved

    return _ENV_PLACEHOLDER_RE.sub(_sub, value)
