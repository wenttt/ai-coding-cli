"""MCP bridge layer. See ADR-0013 + ADR-0030 §MCP Bridge in Lite.

Public exports:
    - MCPBridgeConfig, MCPBridgesYAML: config models
    - load_bridges_yaml: YAML loader with ${env:VAR} resolution
    - MCPBridge: subprocess + ClientSession wrapper
    - MCPBridgeManager: owns N bridges, registers their tools into ToolRegistry
    - BridgeStatus: online | offline | degraded
"""

from __future__ import annotations

from ._bridge import BridgeStatus, MCPBridge
from ._config import (
    MCPBridgeConfig,
    MCPBridgesYAML,
    MCPToolOverride,
    load_bridges_yaml,
    resolve_env_placeholders,
)
from ._manager import MCPBridgeManager

__all__ = [
    "MCPBridgeConfig",
    "MCPBridgesYAML",
    "MCPToolOverride",
    "load_bridges_yaml",
    "resolve_env_placeholders",
    "MCPBridge",
    "BridgeStatus",
    "MCPBridgeManager",
]
