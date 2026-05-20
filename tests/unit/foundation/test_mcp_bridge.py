"""MCP bridge tests. See ADR-0013 + ADR-0030 §MCP Bridge in Lite."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ai_coding_cli.foundation.tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
)
from ai_coding_cli.foundation.tools.mcp import (
    BridgeStatus,
    MCPBridge,
    MCPBridgeConfig,
    MCPBridgeManager,
    MCPBridgesYAML,
    MCPToolOverride,
    load_bridges_yaml,
    resolve_env_placeholders,
)


# ---------------------------------------------------------------------------
# Config loader + env placeholder tests
# ---------------------------------------------------------------------------


def test_load_bridges_yaml_returns_empty_on_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.yaml"
    assert load_bridges_yaml(missing).bridges == []
    assert load_bridges_yaml(None).bridges == []


def test_load_bridges_yaml_parses_valid(tmp_path: Path) -> None:
    path = tmp_path / "mcp_bridges.yaml"
    path.write_text(
        """
bridges:
  - name: alpha
    transport: stdio
    command: /usr/local/bin/alpha-server
    args: ["--config", "/etc/alpha.yaml"]
    env_whitelist:
      ALPHA_TOKEN: "${env:ALPHA_TOKEN}"
    tools_namespace: alpha
    tool_overrides:
      format_log:
        side_effects: read_only
        requires_confirmation: false
""",
        encoding="utf-8",
    )
    yml = load_bridges_yaml(path)
    assert len(yml.bridges) == 1
    bridge = yml.bridges[0]
    assert bridge.name == "alpha"
    assert bridge.tools_namespace == "alpha"
    assert "format_log" in bridge.tool_overrides
    assert bridge.tool_overrides["format_log"].side_effects == SideEffectClass.READ_ONLY


def test_duplicate_bridge_names_raise(tmp_path: Path) -> None:
    path = tmp_path / "mcp_bridges.yaml"
    path.write_text(
        """
bridges:
  - name: a
    command: /bin/true
  - name: a
    command: /bin/true
""",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_bridges_yaml(path)


def test_resolve_env_placeholders_substitutes_known_vars() -> None:
    out = resolve_env_placeholders(
        {"TOKEN": "${env:MY_TOKEN}", "LITERAL": "abc"},
        env={"MY_TOKEN": "secret"},
    )
    assert out == {"TOKEN": "secret", "LITERAL": "abc"}


def test_resolve_env_placeholders_missing_becomes_empty() -> None:
    out = resolve_env_placeholders(
        {"TOKEN": "${env:NOT_SET}"},
        env={"OTHER": "x"},
    )
    assert out == {"TOKEN": ""}


# ---------------------------------------------------------------------------
# Bridge: sandbox env
# ---------------------------------------------------------------------------


def test_bridge_builds_sandboxed_env_only_path_and_whitelist() -> None:
    cfg = MCPBridgeConfig(
        name="b1",
        command="/bin/true",
        env_whitelist={"COMPANY_TOKEN": "${env:COMPANY_TOKEN}"},
    )
    bridge = MCPBridge(
        cfg,
        env_source={
            "PATH": "/usr/bin:/bin",
            "COMPANY_TOKEN": "from-daemon",
            "OPENAI_API_KEY": "must-not-leak",
            "JIRA_API_TOKEN": "must-not-leak",
        },
    )
    env = bridge._build_sandboxed_env()  # noqa: SLF001
    assert env == {"PATH": "/usr/bin:/bin", "COMPANY_TOKEN": "from-daemon"}
    assert "OPENAI_API_KEY" not in env
    assert "JIRA_API_TOKEN" not in env


def test_bridge_sandboxed_env_omits_empty_resolved_values() -> None:
    cfg = MCPBridgeConfig(
        name="b1",
        command="/bin/true",
        env_whitelist={"COMPANY_TOKEN": "${env:NOT_SET}"},
    )
    bridge = MCPBridge(cfg, env_source={"PATH": "/bin"})
    env = bridge._build_sandboxed_env()  # noqa: SLF001
    assert env == {"PATH": "/bin"}


# ---------------------------------------------------------------------------
# Bridge: lifecycle with fake stream/session
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str, description: str, schema: dict[str, Any] | None = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {}


class _FakeListToolsResponse:
    def __init__(self, tools: list[_FakeTool]) -> None:
        self.tools = tools


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [type("Item", (), {"text": text})()]
        self.isError = is_error


class _FakeSession:
    def __init__(
        self,
        tools: list[_FakeTool],
        *,
        call_response: _FakeCallResult | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._tools = tools
        self._call_response = call_response or _FakeCallResult("default")
        self._raise = raise_on_call
        self.initialize_called = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def initialize(self) -> None:
        self.initialize_called = True

    async def list_tools(self) -> _FakeListToolsResponse:
        return _FakeListToolsResponse(self._tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> _FakeCallResult:
        self.calls.append((name, dict(arguments)))
        if self._raise is not None:
            raise self._raise
        return self._call_response


def _fake_stream_cm():
    @asynccontextmanager
    async def _cm() -> Any:
        yield (object(), object())  # placeholder streams

    return _cm()


async def test_bridge_starts_and_lists_tools() -> None:
    cfg = MCPBridgeConfig(name="alpha", command="/bin/true", tools_namespace="alpha")
    fake_session = _FakeSession([_FakeTool("ping", "Ping the bridge.")])
    bridge = MCPBridge(
        cfg,
        stream_factory=lambda _cfg, _env: _fake_stream_cm(),
        session_factory=lambda _r, _w: fake_session,
        env_source={"PATH": "/bin"},
    )

    await bridge.start()
    try:
        assert bridge.status == BridgeStatus.ONLINE
        assert fake_session.initialize_called is True
        assert [t["name"] for t in bridge.listed_tools] == ["ping"]
    finally:
        await bridge.stop()
    assert bridge.status == BridgeStatus.STOPPED


async def test_bridge_call_tool_round_trips() -> None:
    cfg = MCPBridgeConfig(name="alpha", command="/bin/true", tools_namespace="alpha")
    fake_session = _FakeSession(
        [_FakeTool("echo", "Echo.")],
        call_response=_FakeCallResult("hello world"),
    )
    bridge = MCPBridge(
        cfg,
        stream_factory=lambda _cfg, _env: _fake_stream_cm(),
        session_factory=lambda _r, _w: fake_session,
        env_source={"PATH": "/bin"},
    )
    await bridge.start()
    try:
        result = await bridge.call_tool("echo", {"text": "hi"})
        assert result == {"content": "hello world", "is_error": False}
        assert fake_session.calls == [("echo", {"text": "hi"})]
    finally:
        await bridge.stop()


async def test_bridge_marks_degraded_after_max_reconnects() -> None:
    cfg = MCPBridgeConfig(
        name="bad",
        command="/bin/true",
        reconnect_max_attempts=2,
        reconnect_base_seconds=0.01,
        reconnect_max_seconds=0.05,
    )

    @asynccontextmanager
    async def failing_stream(_cfg: Any, _env: Any) -> Any:
        raise RuntimeError("subprocess won't launch")
        yield None  # pragma: no cover - unreachable

    bridge = MCPBridge(
        cfg,
        stream_factory=lambda _cfg, _env: failing_stream(_cfg, _env),
        session_factory=lambda _r, _w: _FakeSession([]),
        env_source={"PATH": "/bin"},
    )
    await bridge.start()
    assert bridge.status == BridgeStatus.DEGRADED
    with pytest.raises(RuntimeError, match="degraded"):
        await bridge.call_tool("anything", {})
    await bridge.stop()


# ---------------------------------------------------------------------------
# Manager: registration into ToolRegistry
# ---------------------------------------------------------------------------


class _FakeBridge:
    """Test double for MCPBridge that uses our FakeSession directly."""

    def __init__(self, cfg: MCPBridgeConfig, env_source=None) -> None:
        self._cfg = cfg
        self._status = BridgeStatus.OFFLINE
        self._tools: list[dict[str, Any]] = [
            {"name": "ping", "description": "Ping.", "inputSchema": {}},
            {"name": "format_log", "description": "Format.", "inputSchema": {}},
        ]

    @property
    def name(self) -> str:
        return self._cfg.name

    @property
    def config(self) -> MCPBridgeConfig:
        return self._cfg

    @property
    def status(self) -> BridgeStatus:
        return self._status

    @property
    def listed_tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def start(self) -> None:
        self._status = BridgeStatus.ONLINE

    async def stop(self) -> None:
        self._status = BridgeStatus.STOPPED

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {"content": f"{name}:{arguments}", "is_error": False}


async def test_manager_registers_namespaced_tools() -> None:
    cfg = MCPBridgeConfig(
        name="alpha",
        command="/bin/true",
        tools_namespace="alpha",
        tool_overrides={
            "ping": MCPToolOverride(
                side_effects=SideEffectClass.READ_ONLY,
                requires_confirmation=False,
            ),
        },
    )
    registry = ToolRegistry()
    manager = MCPBridgeManager(
        yaml_config=MCPBridgesYAML(bridges=[cfg]),
        tool_registry=registry,
        bridge_factory=_FakeBridge,
    )
    await manager.start_all()
    try:
        assert registry.has("alpha.ping")
        assert registry.has("alpha.format_log")
        # Override applied
        ping_tool = registry.get("alpha.ping")
        assert ping_tool.side_effects == SideEffectClass.READ_ONLY
        assert ping_tool.requires_confirmation is False
        # Default conservative classification
        fmt_tool = registry.get("alpha.format_log")
        assert fmt_tool.side_effects == SideEffectClass.EXTERNAL_WRITE
        assert fmt_tool.requires_confirmation is True
    finally:
        await manager.stop_all()
    # Tools are unregistered on stop
    assert not registry.has("alpha.ping")
    assert not registry.has("alpha.format_log")


async def test_manager_skips_disabled_bridges() -> None:
    cfg = MCPBridgeConfig(
        name="alpha", command="/bin/true", auto_start=False,
    )
    registry = ToolRegistry()
    manager = MCPBridgeManager(
        yaml_config=MCPBridgesYAML(bridges=[cfg]),
        tool_registry=registry,
        bridge_factory=_FakeBridge,
    )
    await manager.start_all()
    assert "alpha" not in manager.bridges
    await manager.stop_all()
