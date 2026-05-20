"""Compactor unit tests. See ADR-0011 (Lite MicroCompact)."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding_cli.foundation.compactor import (
    CompactionResult,
    Compactor,
    CompactorConfig,
)
from ai_coding_cli.foundation.llm import MockAdapter


def _system_prefix() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "tier1 system prompt"},
        {"role": "system", "content": "tier2 static prefix"},
    ]


def _user(msg: str) -> dict[str, Any]:
    return {"role": "user", "content": msg}


def _assistant_with_tool_call(call_id: str, tool_name: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": "{}"}}
        ],
    }


def _tool_result(call_id: str, tool_name: str, body: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": body,
    }


@pytest.fixture
def compactor() -> Compactor:
    return Compactor(MockAdapter(), CompactorConfig(preserve_recent_turns=2))


def test_compactor_noop_when_no_triggers(compactor: Compactor) -> None:
    messages = _system_prefix() + [_user("hi")]
    new_messages, result = compactor.maybe_compact(messages, turn_index=1)
    assert result.triggered is False
    assert new_messages == messages
    assert result.tool_messages_dropped == 0


def test_micro_compact_drops_large_low_value_tool_results(compactor: Compactor) -> None:
    big_body = "x" * 5_000
    messages = (
        _system_prefix()
        + [_user("design auth")]
        + [
            _assistant_with_tool_call("c1", "read_repo_file"),
            _tool_result("c1", "read_repo_file", big_body),
            _assistant_with_tool_call("c2", "read_repo_file"),
            _tool_result("c2", "read_repo_file", big_body),
            _assistant_with_tool_call("c3", "list_repo_files"),
            _tool_result("c3", "list_repo_files", big_body),
            # Keep the last 2 assistant turns intact
            _assistant_with_tool_call("c4", "find_relevant_modules"),
            _tool_result("c4", "find_relevant_modules", big_body),
            _assistant_with_tool_call("c5", "find_relevant_modules"),
            _tool_result("c5", "find_relevant_modules", big_body),
        ]
    )
    new_messages, result = compactor.maybe_compact(messages, turn_index=3)
    assert result.triggered is True
    assert result.tool_messages_dropped >= 1
    assert result.placeholder_inserted is True
    # Static prefix preserved
    assert new_messages[0]["content"] == "tier1 system prompt"
    assert new_messages[1]["content"] == "tier2 static prefix"
    # First user preserved
    assert any(m == _user("design auth") for m in new_messages)
    # Placeholder inserted somewhere
    assert any(
        m.get("role") == "system" and m.get("content", "").startswith("[COMPACTED:")
        for m in new_messages
    )
    # Last 2 assistants + their tool results preserved verbatim
    assert any(
        m.get("role") == "assistant"
        and m.get("tool_calls", [{}])[0].get("id") == "c5"
        for m in new_messages
    )


def test_micro_compact_preserves_error_tool_results(compactor: Compactor) -> None:
    error_body = "[ERROR] file not found"
    big_body = "x" * 5_000
    messages = (
        _system_prefix()
        + [_user("design")]
        + [
            _assistant_with_tool_call("c1", "read_repo_file"),
            _tool_result("c1", "read_repo_file", error_body),
            _assistant_with_tool_call("c2", "read_repo_file"),
            _tool_result("c2", "read_repo_file", big_body),
            _assistant_with_tool_call("c3", "read_repo_file"),
            _tool_result("c3", "read_repo_file", big_body),
            _assistant_with_tool_call("c4", "read_repo_file"),
            _tool_result("c4", "read_repo_file", big_body),
        ]
    )
    new_messages, _ = compactor.maybe_compact(messages, turn_index=3)
    # Error result must survive
    assert any(m.get("content") == error_body for m in new_messages)


def test_micro_compact_skips_when_not_enough_assistants(compactor: Compactor) -> None:
    """Below preserve_recent_turns assistants -> nothing to drop."""
    messages = (
        _system_prefix()
        + [_user("go")]
        + [
            _assistant_with_tool_call("c1", "read_repo_file"),
            _tool_result("c1", "read_repo_file", "x" * 5_000),
        ]
    )
    new_messages, result = compactor.maybe_compact(messages, turn_index=3)
    assert result.tool_messages_dropped == 0


def test_micro_compact_respects_droppable_allowlist(compactor: Compactor) -> None:
    """A non-allowlisted tool's large output must not be dropped."""
    big_body = "x" * 5_000
    messages = (
        _system_prefix()
        + [_user("design")]
        + [
            _assistant_with_tool_call("c1", "create_jira_ticket"),
            _tool_result("c1", "create_jira_ticket", big_body),
            _assistant_with_tool_call("c2", "read_repo_file"),
            _tool_result("c2", "read_repo_file", big_body),
            _assistant_with_tool_call("c3", "find_relevant_modules"),
            _tool_result("c3", "find_relevant_modules", big_body),
        ]
    )
    new_messages, _ = compactor.maybe_compact(messages, turn_index=3)
    # The create_jira_ticket result must survive (not in droppable list).
    assert any(m.get("name") == "create_jira_ticket" for m in new_messages)
