"""MCP client — connects to the ai-coding-workflow MCP server.

Spawns the MCP server as a subprocess over stdio, discovers its tools,
and exposes them in OpenAI tool-calling format. The agent loop in
`agent.py` consumes these tools.

Two responsibilities:

1. `list_tools_as_openai_format()` — returns the server's tools shaped
   for the OpenAI `tools` parameter.

2. `call_tool(name, arguments)` — proxies a tool call to the MCP
   server and returns the result content.

Lifecycle: this is an async context manager. Use it inside an `async with`
block so the subprocess is cleaned up reliably.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import Config

log = logging.getLogger(__name__)


class MCPClient:
    """Async context manager wrapping an MCP stdio client + session."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: ClientSession | None = None
        self._exit_stack: Any = None
        self._tools_cache: list[dict[str, Any]] | None = None

    async def __aenter__(self) -> MCPClient:
        # Build child process env: inherit current env + forward MCP_SERVER_ENV_*
        # vars (stripped of the prefix) so the MCP server gets its own JIRA_*,
        # GITHUB_*, WORKSPACE_PATH, etc.
        child_env = dict(os.environ)
        for k, v in self.config.mcp_server_env.items():
            child_env[k] = v

        params = StdioServerParameters(
            command=self.config.mcp_server_command,
            args=self.config.mcp_server_args,
            env=child_env,
        )

        # Open the stdio transport and session, keeping the AsyncExitStack so
        # cleanup happens in __aexit__.
        from contextlib import AsyncExitStack

        self._exit_stack = AsyncExitStack()
        stdio_transport = await self._exit_stack.enter_async_context(stdio_client(params))
        read_stream, write_stream = stdio_transport
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        log.info("MCP session initialized with server %s", self.config.mcp_server_command)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
        self._session = None
        self._tools_cache = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError(
                "MCPClient is not entered. Use `async with MCPClient(config) as mcp:`."
            )
        return self._session

    async def list_tools_as_openai_format(self) -> list[dict[str, Any]]:
        """Discover tools from the MCP server, formatted for OpenAI tool calls."""
        if self._tools_cache is not None:
            return self._tools_cache

        result = await self.session.list_tools()
        out: list[dict[str, Any]] = []
        for tool in result.tools:
            out.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                },
            })
        self._tools_cache = out
        log.info("Discovered %d MCP tools", len(out))
        return out

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Invoke a tool by name. Returns the textual content of the response.

        MCP tool results can include multiple content blocks (text, image,
        resource). For LLM consumption we serialize them to a single string —
        text blocks concatenated, structured content JSON-encoded.
        """
        log.debug("Calling tool: %s args=%s", name, arguments)
        result = await self.session.call_tool(name, arguments=arguments)

        parts: list[str] = []
        for block in result.content:
            # block.type is one of: text, image, resource, embedded_resource
            block_type = getattr(block, "type", None)
            if block_type == "text":
                parts.append(getattr(block, "text", "") or "")
            else:
                # Best-effort serialization for non-text blocks
                try:
                    parts.append(json.dumps(block.model_dump(), ensure_ascii=False))
                except Exception:
                    parts.append(repr(block))

        if result.isError:
            return f"[TOOL ERROR] {''.join(parts)}"
        return "\n".join(p for p in parts if p)
