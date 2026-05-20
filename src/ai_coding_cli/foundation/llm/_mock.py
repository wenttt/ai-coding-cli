"""MockAdapter for tests + Replay. See ADR-0014 + ADR-0018."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ._adapter import CacheHints, LLMResponse, ToolCall

ResponseMatcher = Callable[[list[dict[str, Any]]], bool]


class MockAdapter:
    """Deterministic LLM adapter. Tests prime responses via `queue_response`.

    Two matching modes:
    - `queue_response(response=...)` queues a one-shot response (FIFO order
      across all queued items).
    - `add_matcher(when=callable, response=...)` registers a predicate; the
      first matching matcher (in registration order) wins.

    `count_tokens` uses `len(text) // 4` as a deterministic estimate.
    """

    provider_name = "mock"
    supports_tool_calling = True
    supports_streaming = False
    supports_prompt_cache_hints = False

    def __init__(self, model_name: str = "mock-model-1") -> None:
        self.model_name = model_name
        self._queue: list[LLMResponse] = []
        self._matchers: list[tuple[ResponseMatcher, LLMResponse]] = []
        self._compaction_summary: str = "[mock compaction summary]"
        self.calls: list[dict[str, Any]] = []

    # ----- Priming API -----

    def queue_response(self, response: LLMResponse) -> None:
        self._queue.append(response)

    def add_matcher(
        self,
        *,
        when: ResponseMatcher,
        response: LLMResponse,
    ) -> None:
        self._matchers.append((when, response))

    def set_compaction_summary(self, summary: str) -> None:
        self._compaction_summary = summary

    # ----- LLMAdapter interface -----

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 300.0,
        cache_hints: CacheHints | None = None,
    ) -> LLMResponse:
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })

        # Matchers win over queue (more specific).
        for matcher, response in self._matchers:
            if matcher(messages):
                return response

        if self._queue:
            return self._queue.pop(0)

        # Default fallback: empty assistant message; signals "stop".
        return LLMResponse(
            content="(mock: no response primed)",
            tool_calls=[],
            finish_reason="stop",
            prompt_tokens=self.count_tokens(messages),
            completion_tokens=10,
            total_tokens=self.count_tokens(messages) + 10,
        )

    async def summarize_for_compaction(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,
        max_tokens: int = 4000,
    ) -> str:
        return self._compaction_summary

    def count_tokens(self, content: str | list[dict[str, Any]]) -> int:
        if isinstance(content, str):
            return max(1, len(content) // 4)
        total = 0
        for msg in content:
            for value in msg.values():
                if isinstance(value, str):
                    total += max(1, len(value) // 4)
                else:
                    total += max(1, len(json.dumps(value, ensure_ascii=False, default=str)) // 4)
        return total


# ---------------------------------------------------------------------------
# Convenience response builders for tests
# ---------------------------------------------------------------------------


def text_response(
    text: str,
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
) -> LLMResponse:
    return LLMResponse(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def tool_call_response(
    tool_calls: list[ToolCall],
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
