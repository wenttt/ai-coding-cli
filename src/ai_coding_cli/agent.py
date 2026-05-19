"""ReAct agent loop.

One invocation = one user message in, one final assistant message out.
Between them, the loop iterates:

    1. Send messages to LLM
    2. If the response has tool_calls -> call each tool, append results to
       messages, go back to step 1.
    3. If the response is plain content (no tool_calls) -> done, return.

Bounded by `agent_max_turns` to prevent runaway loops. Each turn = one
LLM call; one LLM call may dispatch many tool calls in parallel.

The agent itself is stateless — caller manages message history if multi-turn
conversation is desired.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from .config import Config
from .llm import LLM
from .tools.mcp_client import MCPClient

log = logging.getLogger(__name__)
console = Console()


@dataclass
class AgentResult:
    """Result of a full agent run."""

    final_message: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turns_used: int = 0
    tool_calls_made: int = 0
    hit_turn_limit: bool = False


class Agent:
    """A ReAct-style agent over OpenAI-compatible LLM + MCP tools."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.llm = LLM(config)

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Run the loop until the LLM stops requesting tools, or limit hit.

        Args:
            system_prompt: prepended as a system message every run.
            user_message: the new user instruction (e.g. "start working on KAN-4").
            prior_messages: optional message history to continue from.

        Returns AgentResult with the final assistant text and full message log.
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if prior_messages:
            messages.extend(prior_messages)
        messages.append({"role": "user", "content": user_message})

        turns = 0
        tool_calls_made = 0

        async with MCPClient(self.config) as mcp:
            tools = await mcp.list_tools_as_openai_format()
            console.log(f"[dim]Loaded {len(tools)} tools from MCP server[/dim]")

            for turn in range(self.config.agent_max_turns):
                turns = turn + 1

                # 1. Ask the LLM
                with console.status(f"[bold cyan]Thinking (turn {turns})...[/bold cyan]"):
                    response = self.llm.complete(messages=messages, tools=tools)

                # 2. Convert response to a dict we can store in messages
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if response.content:
                    assistant_msg["content"] = response.content
                if response.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in response.tool_calls
                    ]
                messages.append(assistant_msg)

                # 3. If there are tool calls, dispatch and continue
                if response.tool_calls:
                    for tc in response.tool_calls:
                        tool_calls_made += 1
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError as exc:
                            tool_result = f"[ARGUMENT PARSE ERROR] {exc}: {tc.function.arguments}"
                        else:
                            console.log(f"[yellow]Tool[/yellow] {name}({_summarize_args(args)})")
                            try:
                                tool_result = await mcp.call_tool(name, args)
                            except Exception as exc:
                                tool_result = f"[TOOL EXCEPTION] {type(exc).__name__}: {exc}"
                                log.exception("Tool call failed")

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result[:50_000],  # cap to avoid token blowup
                        })
                    # Loop again — LLM may want more tools or have a final answer
                    continue

                # 4. No tool calls — LLM has a final answer
                final = response.content or "(no content)"
                console.print()
                console.print(final)
                return AgentResult(
                    final_message=final,
                    messages=messages,
                    turns_used=turns,
                    tool_calls_made=tool_calls_made,
                    hit_turn_limit=False,
                )

            # Exhausted turn budget
            console.print()
            console.print(
                f"[red]Agent hit turn limit ({self.config.agent_max_turns}) "
                "without producing a final answer.[/red]"
            )
            return AgentResult(
                final_message="(hit turn limit)",
                messages=messages,
                turns_used=turns,
                tool_calls_made=tool_calls_made,
                hit_turn_limit=True,
            )

    def run_sync(
        self,
        system_prompt: str,
        user_message: str,
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Synchronous wrapper around run() for CLI use."""
        return asyncio.run(self.run(system_prompt, user_message, prior_messages))


def _summarize_args(args: dict[str, Any]) -> str:
    """Make a short readable form of tool args for terminal display."""
    parts: list[str] = []
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 60:
            parts.append(f"{k}=<{len(v)} chars>")
        else:
            parts.append(f"{k}={v!r}")
    return ", ".join(parts)
