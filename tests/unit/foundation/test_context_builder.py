"""ContextBuilder unit tests. See ADR-0010."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ai_coding_cli.foundation.context import (
    ContextBuilder,
    LoadedSkill,
    RepoFacts,
    load_system_prompt,
)
from ai_coding_cli.foundation.llm._adapter import LLMResponse, ToolCall
from ai_coding_cli.foundation.session import (
    ConversationView,
    Message,
    SessionView,
)
from ai_coding_cli.foundation.tools import ToolResult


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_session(tmp_path: Path) -> SessionView:
    return SessionView(
        id="sess-1",
        user_id="me",
        jira_key="PROJ-1",
        primary_project_key="PROJ",
        workspace_root=tmp_path,
        mode="brownfield",
        is_cross_project=False,
        status="active",
        created_at=_now(),
        last_active_at=_now(),
        metadata={},
    )


def _make_conversation(messages: list[Message] | None = None) -> ConversationView:
    return ConversationView(
        id="conv-1",
        session_id="sess-1",
        stage="design",
        revision=1,
        status="running",
        started_at=_now(),
        ended_at=None,
        messages=messages or [],
        turn_count=0,
        tool_call_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cache_hit_tokens=0,
    )


def test_load_system_prompt_returns_nonempty() -> None:
    prompt = load_system_prompt()
    assert len(prompt) > 100
    assert "ReAct loop" in prompt


def test_build_initial_returns_three_tiers(tmp_path: Path) -> None:
    builder = ContextBuilder()
    session = _make_session(tmp_path)
    conversation = _make_conversation()
    repo_facts = RepoFacts(languages=["Python"], frameworks=["FastAPI"])

    messages = builder.build_initial(
        session=session,
        conversation=conversation,
        new_user_message="Design the OAuth flow.",
        conventions=None,
        repo_facts=repo_facts,
        loaded_skills=[],
        operation_log_path="docs/operations/PROJ-1/01-design-v1.md",
    )

    assert messages[0]["role"] == "system"
    assert "ReAct loop" in messages[0]["content"]

    assert messages[1]["role"] == "system"
    static = messages[1]["content"]
    assert "[REPO FACTS]" in static
    assert "Python" in static
    assert "FastAPI" in static
    assert "[SESSION]" in static
    assert "PROJ-1" in static
    assert "docs/operations/PROJ-1/01-design-v1.md" in static

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Design the OAuth flow."


def test_build_initial_includes_conventions_when_present(tmp_path: Path) -> None:
    builder = ContextBuilder()
    messages = builder.build_initial(
        session=_make_session(tmp_path),
        conversation=_make_conversation(),
        new_user_message="go",
        conventions="## Tests\n- pytest mandatory",
        repo_facts=RepoFacts(),
    )
    assert "[PROJECT CONVENTIONS]" in messages[1]["content"]
    assert "pytest mandatory" in messages[1]["content"]


def test_build_initial_skips_empty_skills(tmp_path: Path) -> None:
    builder = ContextBuilder()
    messages = builder.build_initial(
        session=_make_session(tmp_path),
        conversation=_make_conversation(),
        new_user_message="go",
        conventions=None,
        repo_facts=RepoFacts(),
        loaded_skills=[
            LoadedSkill(name="skill-a", content=""),
            LoadedSkill(name="skill-b", content="actual content"),
        ],
    )
    static = messages[1]["content"]
    assert "[SKILL: skill-b]" in static
    assert "[SKILL: skill-a]" not in static


def test_append_assistant_message_serializes_tool_calls(tmp_path: Path) -> None:
    builder = ContextBuilder()
    messages: list = []
    response = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id="call_1", name="read_repo_file", arguments={"path": "x.py"})
        ],
        finish_reason="tool_calls",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    builder.append_assistant_message(messages, response)
    entry = messages[-1]
    assert entry["role"] == "assistant"
    assert entry["tool_calls"][0]["function"]["name"] == "read_repo_file"
    args = json.loads(entry["tool_calls"][0]["function"]["arguments"])
    assert args == {"path": "x.py"}


def test_append_tool_results_appends_one_message_per_call() -> None:
    builder = ContextBuilder()
    messages: list = [{"role": "user", "content": "go"}]
    results = [
        (
            "call_1",
            ToolResult.success(
                tool_name="read_repo_file",
                invocation_id="i1",
                content="file contents",
            ),
        ),
        (
            "call_2",
            ToolResult.error(
                tool_name="git_status",
                invocation_id="i2",
                message="not a git repo",
            ),
        ),
    ]
    builder.append_tool_results(messages, results)
    assert messages[-2]["role"] == "tool"
    assert messages[-2]["tool_call_id"] == "call_1"
    assert messages[-1]["tool_call_id"] == "call_2"
    assert "[ERROR]" in messages[-1]["content"]


def test_inject_loaded_skill_appends_tagged_system_message() -> None:
    builder = ContextBuilder()
    messages: list = [{"role": "user", "content": "go"}]
    builder.inject_loaded_skill(
        messages,
        LoadedSkill(name="mid-loop-skill", content="body"),
        loaded_at_turn=3,
    )
    assert messages[-1]["role"] == "system"
    assert "[SKILL: mid-loop-skill" in messages[-1]["content"]
    assert "loaded mid-conversation at turn 3" in messages[-1]["content"]
