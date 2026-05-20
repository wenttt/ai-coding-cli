"""MCPBridgeManager: starts N bridges + registers their tools. See ADR-0030."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, create_model

from .._context import ToolContext
from .._registry import ToolRegistry
from .._result import ToolResult
from .._side_effects import SideEffectClass
from .._tool import Tool
from ._bridge import BridgeStatus, MCPBridge
from ._config import MCPBridgeConfig, MCPBridgesYAML, MCPToolOverride

logger = logging.getLogger(__name__)


class MCPBridgeManager:
    """Owns N MCPBridge instances + their registered Tool wrappers.

    `start_all()` spawns each bridge in parallel and registers tools as
    `{namespace}.{tool_name}` (or just `{tool_name}` when namespace is empty).
    `stop_all()` is the reverse.
    """

    def __init__(
        self,
        *,
        yaml_config: MCPBridgesYAML,
        tool_registry: ToolRegistry,
        env_source: dict[str, str] | None = None,
        bridge_factory: type[MCPBridge] = MCPBridge,
    ) -> None:
        self._yaml = yaml_config
        self._registry = tool_registry
        self._env_source = env_source
        self._bridge_factory = bridge_factory
        self._bridges: dict[str, MCPBridge] = {}
        self._registered_names: dict[str, list[str]] = {}

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    async def start_all(self) -> None:
        coros = [self._start_one(cfg) for cfg in self._yaml.bridges if cfg.auto_start]
        if not coros:
            logger.info("mcp.manager_no_auto_start_bridges")
            return
        await asyncio.gather(*coros, return_exceptions=False)

    async def stop_all(self) -> None:
        for tool_names in self._registered_names.values():
            for tname in tool_names:
                self._registry.unregister(tname)
        self._registered_names.clear()

        await asyncio.gather(
            *(b.stop() for b in self._bridges.values()),
            return_exceptions=True,
        )
        self._bridges.clear()

    @property
    def bridges(self) -> dict[str, MCPBridge]:
        return dict(self._bridges)

    def bridge_status(self) -> dict[str, BridgeStatus]:
        return {name: b.status for name, b in self._bridges.items()}

    # -----------------------------------------------------------------
    # Per-bridge startup
    # -----------------------------------------------------------------

    async def _start_one(self, cfg: MCPBridgeConfig) -> None:
        bridge = self._bridge_factory(
            cfg,
            env_source=self._env_source,
        )
        self._bridges[cfg.name] = bridge
        await bridge.start()
        if bridge.status != BridgeStatus.ONLINE:
            logger.warning(
                "mcp.bridge_not_online_after_start name=%s status=%s",
                cfg.name,
                bridge.status.value,
            )
            return
        # Register tools.
        names: list[str] = []
        for remote_tool in bridge.listed_tools:
            tool = _build_tool_for_remote(bridge, cfg, remote_tool)
            if tool is None:
                continue
            try:
                self._registry.register(tool)
            except ValueError as exc:
                logger.warning(
                    "mcp.tool_register_failed bridge=%s tool=%s: %s",
                    cfg.name,
                    tool.name,
                    exc,
                )
                continue
            names.append(tool.name)
        self._registered_names[cfg.name] = names


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def _build_tool_for_remote(
    bridge: MCPBridge,
    cfg: MCPBridgeConfig,
    remote_tool: dict[str, Any],
) -> Tool | None:
    name = remote_tool.get("name")
    description = remote_tool.get("description") or f"MCP tool {name!r} via {cfg.name!r}."
    if not name:
        logger.warning("mcp.remote_tool_no_name bridge=%s", cfg.name)
        return None

    input_schema = (
        remote_tool.get("inputSchema") or remote_tool.get("input_schema") or {}
    )
    input_model = _build_input_model(name, input_schema, namespace=cfg.tools_namespace)

    override = cfg.tool_overrides.get(name)
    side_effects = (
        override.side_effects
        if override and override.side_effects is not None
        else SideEffectClass.EXTERNAL_WRITE  # conservative default per ADR-0030
    )
    requires_confirmation = (
        override.requires_confirmation
        if override and override.requires_confirmation is not None
        else True
    )
    visible = (
        override.visible_to_agent
        if override and override.visible_to_agent is not None
        else True
    )

    full_name = f"{cfg.tools_namespace}.{name}" if cfg.tools_namespace else name
    impl = _make_impl(bridge, name)

    return Tool(
        name=full_name,
        description=description,
        input_model=input_model,
        side_effects=side_effects,
        requires_confirmation=requires_confirmation,
        visible_to_agent=visible,
        timeout_seconds=cfg.timeout_seconds,
        impl=impl,
    )


def _make_impl(bridge: MCPBridge, remote_name: str):  # type: ignore[no-untyped-def]
    """Build the per-tool impl that forwards to bridge.call_tool."""

    async def _impl(args: BaseModel, _ctx: ToolContext) -> dict[str, Any]:
        # The MCP server expects a plain dict of arguments.
        if isinstance(args, BaseModel):
            arguments = args.model_dump(exclude_unset=False, mode="python")
        else:
            arguments = dict(args)  # type: ignore[arg-type]
        return await bridge.call_tool(remote_name, arguments)

    return _impl


def _build_input_model(
    tool_name: str,
    schema: dict[str, Any],
    *,
    namespace: str,
) -> type[BaseModel]:
    """Build a Pydantic model for the remote tool's input schema.

    Lite uses a permissive `extra=allow` BaseModel with no validation — the
    bridge enforces schema. A future iteration can translate JSONSchema to
    a strict Pydantic model.
    """
    safe_namespace = namespace or "default"
    model_name = f"MCP_{safe_namespace}_{tool_name}_Args".replace("-", "_")

    class _PermissiveBase(BaseModel):
        model_config = {"extra": "allow"}

    model = create_model(model_name, __base__=_PermissiveBase)
    return model
