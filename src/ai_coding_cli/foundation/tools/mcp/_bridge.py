"""MCPBridge: subprocess + ClientSession on a background asyncio task.

Lifecycle per ADR-0030 §Lifecycle:
- start(): spawn subprocess via stdio_client + ClientSession, listen for
  tool-call requests on an asyncio Queue, dispatch them via session, return
  results back via Futures.
- On subprocess death: exit contexts cleanly, mark offline, schedule
  reconnect with exponential backoff (1s, 2s, 4s, 8s, 16s, capped 30s).
- After `reconnect_max_attempts` failures: mark degraded; tools return
  ToolResult.error("bridge offline") so the LLM sees + adapts.

Concurrency: this is a single-bridge worker. The MCPBridgeManager owns one
of these per configured bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ._config import MCPBridgeConfig, resolve_env_placeholders

logger = logging.getLogger(__name__)


class BridgeStatus(StrEnum):
    OFFLINE = "offline"
    STARTING = "starting"
    ONLINE = "online"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True)
class _CallRequest:
    """One pending tool call awaiting dispatch on the bridge worker task."""

    tool_name: str
    arguments: dict[str, Any]
    future: asyncio.Future[Any]


# A factory callable that returns the (read, write) streams given a bridge
# config. Default uses `stdio_client`; tests inject a stub.
StreamFactory = Callable[
    [MCPBridgeConfig, dict[str, str]],
    Any,  # Returns an async context manager yielding (read, write) streams.
]


# A factory callable that wraps (read, write) streams in a ClientSession.
# Default uses `mcp.ClientSession`; tests inject a stub.
SessionFactory = Callable[[Any, Any], Any]


def _default_stream_factory(
    config: MCPBridgeConfig, env: dict[str, str]
) -> Any:
    params = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=env,
        cwd=config.cwd,
    )
    return stdio_client(params)


def _default_session_factory(read: Any, write: Any) -> Any:
    return ClientSession(read, write)


class MCPBridge:
    """One MCP bridge: subprocess + ClientSession on a worker task."""

    def __init__(
        self,
        config: MCPBridgeConfig,
        *,
        stream_factory: StreamFactory | None = None,
        session_factory: SessionFactory | None = None,
        env_source: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._stream_factory = stream_factory or _default_stream_factory
        self._session_factory = session_factory or _default_session_factory
        self._env_source = env_source

        self._status: BridgeStatus = BridgeStatus.OFFLINE
        self._task: asyncio.Task[None] | None = None
        self._request_queue: asyncio.Queue[_CallRequest] = asyncio.Queue()
        self._ready_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._listed_tools: list[dict[str, Any]] = []
        self._reconnect_attempts = 0

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def config(self) -> MCPBridgeConfig:
        return self._config

    @property
    def status(self) -> BridgeStatus:
        return self._status

    @property
    def listed_tools(self) -> list[dict[str, Any]]:
        return list(self._listed_tools)

    async def start(self) -> None:
        """Spawn the worker task. Returns once the bridge has either become
        ONLINE for the first time OR exhausted its reconnect budget (DEGRADED).
        """
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(
            self._run_worker(), name=f"mcp-bridge[{self._config.name}]"
        )
        # Wait until either ready OR degraded OR stopped.
        await self._ready_event.wait()

    async def stop(self) -> None:
        """Signal the worker to exit cleanly. Returns once the worker task
        has finished.
        """
        self._stop_event.set()
        # Drain pending requests with a clean error so callers don't hang.
        while not self._request_queue.empty():
            try:
                req = self._request_queue.get_nowait()
                if not req.future.done():
                    req.future.set_exception(
                        RuntimeError(f"bridge {self._config.name!r} stopping")
                    )
            except asyncio.QueueEmpty:
                break
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._status = BridgeStatus.STOPPED

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Enqueue a tool call for the worker. Returns the bridge's response.

        Raises RuntimeError when the bridge is degraded / stopped.
        """
        if self._status in (BridgeStatus.DEGRADED, BridgeStatus.STOPPED):
            raise RuntimeError(
                f"bridge {self._config.name!r} is {self._status.value}"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._request_queue.put(
            _CallRequest(tool_name=tool_name, arguments=arguments, future=future)
        )
        try:
            return await asyncio.wait_for(
                future, timeout=self._config.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"bridge {self._config.name!r} timed out after "
                f"{self._config.timeout_seconds}s on tool {tool_name!r}"
            ) from exc

    # -----------------------------------------------------------------
    # Worker
    # -----------------------------------------------------------------

    async def _run_worker(self) -> None:
        """Main loop: connect, dispatch, reconnect on failure."""
        cfg = self._config
        while not self._stop_event.is_set():
            self._status = BridgeStatus.STARTING
            try:
                await self._connect_and_dispatch()
                # Normal exit (stop requested while connected).
                self._status = BridgeStatus.STOPPED
                return
            except asyncio.CancelledError:
                self._status = BridgeStatus.STOPPED
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mcp.bridge_error name=%s attempt=%d: %s",
                    cfg.name,
                    self._reconnect_attempts,
                    exc,
                )
                self._reconnect_attempts += 1
                if self._reconnect_attempts > cfg.reconnect_max_attempts:
                    logger.error(
                        "mcp.bridge_degraded name=%s; max reconnect attempts (%d) reached.",
                        cfg.name,
                        cfg.reconnect_max_attempts,
                    )
                    self._status = BridgeStatus.DEGRADED
                    self._ready_event.set()  # release start() waiter
                    return
                backoff = min(
                    cfg.reconnect_max_seconds,
                    cfg.reconnect_base_seconds * (2 ** (self._reconnect_attempts - 1)),
                )
                logger.info(
                    "mcp.bridge_reconnect_in name=%s seconds=%.1f",
                    cfg.name,
                    backoff,
                )
                self._status = BridgeStatus.OFFLINE
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff
                    )
                    self._status = BridgeStatus.STOPPED
                    return
                except asyncio.TimeoutError:
                    continue
        self._status = BridgeStatus.STOPPED

    async def _connect_and_dispatch(self) -> None:
        cfg = self._config
        env = self._build_sandboxed_env()
        logger.info(
            "mcp.bridge_starting name=%s command=%s env_keys=%s",
            cfg.name,
            cfg.command,
            sorted(env.keys()),
        )

        async with self._stream_factory(cfg, env) as streams:
            read, write = streams
            async with self._session_factory(read, write) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                self._listed_tools = _extract_tools(tools_response)
                logger.info(
                    "mcp.bridge_online name=%s tools=%d",
                    cfg.name,
                    len(self._listed_tools),
                )
                self._status = BridgeStatus.ONLINE
                self._reconnect_attempts = 0
                self._ready_event.set()
                await self._dispatch_loop(session)

    async def _dispatch_loop(self, session: Any) -> None:
        while not self._stop_event.is_set():
            try:
                request = await asyncio.wait_for(
                    self._request_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            try:
                result = await session.call_tool(
                    request.tool_name, request.arguments
                )
            except Exception as exc:  # noqa: BLE001
                if not request.future.done():
                    request.future.set_exception(exc)
                # Connection failures bubble up so the outer reconnect loop kicks in.
                if _is_connection_error(exc):
                    raise
                continue
            if not request.future.done():
                request.future.set_result(_unpack_result(result))

    def _build_sandboxed_env(self) -> dict[str, str]:
        """ADR-0030 §Sandboxed environment.

        Only PATH + explicitly whitelisted vars get forwarded. Placeholders
        like `${env:COMPANY_TOKEN}` are resolved against the daemon's
        environment.
        """
        resolved = resolve_env_placeholders(
            self._config.env_whitelist, env=self._env_source
        )
        path = (self._env_source or {}).get("PATH") if self._env_source else None
        if path is None:
            import os

            path = os.environ.get("PATH", "")
        env: dict[str, str] = {"PATH": path}
        env.update({k: v for k, v in resolved.items() if v != ""})
        return env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_tools(response: Any) -> list[dict[str, Any]]:
    """Normalize the MCP list_tools response into a list of dicts."""
    raw = getattr(response, "tools", None) or response
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            out.append(_tool_to_dict(item))
    return out


def _tool_to_dict(tool_obj: Any) -> dict[str, Any]:
    if isinstance(tool_obj, dict):
        return tool_obj
    # Pydantic model — dump it.
    if hasattr(tool_obj, "model_dump"):
        return tool_obj.model_dump()
    # Plain object — pick known attributes.
    return {
        "name": getattr(tool_obj, "name", "unknown"),
        "description": getattr(tool_obj, "description", ""),
        "inputSchema": getattr(tool_obj, "inputSchema", {}),
    }


def _unpack_result(result: Any) -> dict[str, Any]:
    """Normalize a ClientSession.call_tool result.

    The MCP SDK returns a CallToolResult with `content` (list of TextContent /
    ImageContent / ...). We flatten to {"content": str, "isError": bool, "raw": ...}.
    """
    if isinstance(result, dict):
        return result

    content_items = getattr(result, "content", None) or []
    text_parts: list[str] = []
    for item in content_items:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
        elif isinstance(item, dict) and "text" in item:
            text_parts.append(str(item["text"]))
    return {
        "content": "\n".join(text_parts),
        "is_error": bool(getattr(result, "isError", False)),
    }


def _is_connection_error(exc: Exception) -> bool:
    """Heuristic: classify exceptions that indicate the subprocess died."""
    msg = str(exc).lower()
    return any(
        signal in msg
        for signal in (
            "broken pipe",
            "connection closed",
            "connection lost",
            "session closed",
            "stream closed",
            "exited",
        )
    )
