"""Tests for the LLM Adapter (MockAdapter + factory). See ADR-0014."""

from __future__ import annotations

import pytest

from ai_coding_cli.foundation.config import AdapterConfig
from ai_coding_cli.foundation.llm import (
    MockAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
)
from ai_coding_cli.foundation.llm._mock import text_response, tool_call_response
from ai_coding_cli.foundation.llm._adapter import LLMAdapter, ToolCall


def test_mock_adapter_implements_protocol() -> None:
    adapter = MockAdapter()
    assert isinstance(adapter, LLMAdapter)
    assert adapter.provider_name == "mock"


@pytest.mark.asyncio
async def test_mock_adapter_returns_queued_response() -> None:
    adapter = MockAdapter()
    adapter.queue_response(text_response("hello"))
    result = await adapter.complete(messages=[{"role": "user", "content": "hi"}])
    assert result.content == "hello"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_mock_adapter_returns_tool_call_response() -> None:
    adapter = MockAdapter()
    adapter.queue_response(
        tool_call_response([ToolCall(id="call_1", name="read_repo_file", arguments={"path": "x"})])
    )
    result = await adapter.complete(messages=[{"role": "user", "content": "go"}])
    assert result.tool_calls
    assert result.tool_calls[0].name == "read_repo_file"


@pytest.mark.asyncio
async def test_mock_adapter_matcher_wins_over_queue() -> None:
    adapter = MockAdapter()
    adapter.queue_response(text_response("queue-fallback"))
    adapter.add_matcher(
        when=lambda msgs: any("KAN-4" in str(m) for m in msgs),
        response=text_response("matcher-hit"),
    )
    r1 = await adapter.complete(messages=[{"role": "user", "content": "start KAN-4"}])
    assert r1.content == "matcher-hit"
    r2 = await adapter.complete(messages=[{"role": "user", "content": "do something else"}])
    assert r2.content == "queue-fallback"


def test_mock_adapter_token_count_is_deterministic() -> None:
    adapter = MockAdapter()
    assert adapter.count_tokens("hello world") == max(1, len("hello world") // 4)
    msgs = [{"role": "user", "content": "ab" * 100}]
    assert adapter.count_tokens(msgs) > 0


def test_build_adapter_factory_constructs_mock() -> None:
    cfg = AdapterConfig(kind="mock", model_name="mock-1")
    adapter = build_adapter(cfg)
    assert isinstance(adapter, MockAdapter)


def test_build_adapter_factory_constructs_openai_compat() -> None:
    cfg = AdapterConfig(
        kind="openai-compat",
        model_name="gpt-4o",
        base_url="https://llm.test/v1",  # type: ignore[arg-type]
        api_key="test-key",  # type: ignore[arg-type]
    )
    adapter = build_adapter(cfg)
    assert isinstance(adapter, OpenAICompatibleAdapter)
    assert adapter.model_name == "gpt-4o"


def test_build_adapter_rejects_anthropic_native_in_lite() -> None:
    cfg = AdapterConfig(kind="anthropic-native", model_name="claude-3-5-sonnet")
    with pytest.raises(ValueError, match="Standard profile"):
        build_adapter(cfg)


def test_build_adapter_requires_base_url_for_openai_compat() -> None:
    cfg = AdapterConfig(kind="openai-compat", model_name="x")
    with pytest.raises(ValueError, match="base_url"):
        build_adapter(cfg)
