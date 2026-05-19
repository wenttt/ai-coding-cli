"""LLM provider — thin wrapper over the OpenAI Python SDK.

The SDK speaks the OpenAI chat completions protocol, which is the de
facto standard. Many enterprise LLM gateways implement this protocol
(internal Copilot backends, vLLM servers, Together, Groq, Fireworks,
Anthropic via OpenAI-compat shim, etc.) so pointing `base_url` at the
right endpoint lets this CLI work with whatever LLM the user has.

This module deliberately keeps the surface minimal: one streaming-aware
chat call with tool support. Higher-level concerns (loop control,
message accumulation) live in `agent.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessage

from .config import Config

log = logging.getLogger(__name__)


class LLM:
    """OpenAI-compatible chat client."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = OpenAI(
            base_url=config.openai_base_url,
            api_key=config.openai_api_key,
            timeout=60.0,
            max_retries=2,
        )
        self.model = config.openai_model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> ChatCompletionMessage:
        """Send one chat completion request and return the assistant message.

        `messages` is the full conversation so far in OpenAI format:
        [{"role": "system|user|assistant|tool", "content": "...", ...}]

        `tools` is the OpenAI function-calling schema list, e.g.:
        [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]

        Returns the assistant message verbatim — may contain `content`,
        `tool_calls`, or both. Caller dispatches tool_calls if present.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        log.debug(
            "LLM call: model=%s, n_messages=%d, n_tools=%d",
            self.model,
            len(messages),
            len(tools) if tools else 0,
        )

        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message
