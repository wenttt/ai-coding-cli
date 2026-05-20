"""LLMAdapter Protocol + response shapes. See ADR-0014."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the LLM in a single turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """Response from one `LLMAdapter.complete()` call.

    `content` and `tool_calls` are both possible; the LLM may produce one or
    both. The Agent Core terminates when `tool_calls` is empty (assistant
    has nothing more to do).
    """

    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int = 0
    raw_provider_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class CacheHints:
    """Prompt-cache hints passed to the adapter.

    The Context Layer (ADR-0010) sets `cacheable_prefix_end_index` to point
    at the last message in the stable prefix (Tier 1 + Tier 2). Adapters
    translate this to provider-specific cache markers when supported.
    """

    cacheable_prefix_end_index: int | None
    ephemeral: bool = True


@runtime_checkable
class LLMAdapter(Protocol):
    """Provider-agnostic chat completion + summarization + tokenization."""

    provider_name: str
    model_name: str
    supports_tool_calling: bool
    supports_streaming: bool
    supports_prompt_cache_hints: bool

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 300.0,
        cache_hints: CacheHints | None = None,
    ) -> LLMResponse: ...

    async def summarize_for_compaction(
        self,
        *,
        messages: list[dict[str, Any]],
        instructions: str,
        max_tokens: int = 4000,
    ) -> str: ...

    def count_tokens(self, content: str | list[dict[str, Any]]) -> int: ...
