"""ai-coding-cli — Self-contained AI Coding Agent.

Drives the AI Coding Workflow pipeline (design → implement → review →
test → deploy) from a single command line, without depending on any
IDE Agent (no Copilot, Roo Code, Cursor, etc).

Three layers:

1. `cli`       — Typer-based entry point. `ai-coding "start KAN-4"`.
2. `agent`     — ReAct loop. Sends messages to the LLM, dispatches
                 tool calls, accumulates results, exits when LLM
                 stops requesting tools.
3. `llm`       — Thin wrapper over openai SDK, pointed at any
                 OpenAI-compatible endpoint (company gateway, OpenAI,
                 Anthropic OpenAI-compat shim).
4. `tools.mcp_client` — Spawns the ai-coding-workflow MCP server as
                 a subprocess, exposes its tools to the LLM in the
                 OpenAI function-calling format.
"""

__version__ = "0.1.0"
