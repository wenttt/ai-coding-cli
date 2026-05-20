"""LLM Adapter (provider-agnostic chat + tool calling). See ADR-0014.

Public exports:
    - LLMAdapter (Protocol)
    - LLMResponse, ToolCall, CacheHints
    - OpenAICompatibleAdapter
    - MockAdapter (deterministic; for tests + Replay)
    - build_adapter (factory from AdapterConfig)
"""

from __future__ import annotations

from ._adapter import CacheHints, LLMAdapter, LLMResponse, ToolCall
from ._factory import build_adapter
from ._mock import MockAdapter
from ._openai_compat import OpenAICompatibleAdapter

__all__ = [
    "LLMAdapter",
    "LLMResponse",
    "ToolCall",
    "CacheHints",
    "OpenAICompatibleAdapter",
    "MockAdapter",
    "build_adapter",
]
