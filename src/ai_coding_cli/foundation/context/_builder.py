"""ContextBuilder: assembles the message list for an Agent turn. See ADR-0010.

The Lite implementation supports:
- build_initial(): Tier 1 + Tier 2 + initial user message
- append_user_message / append_assistant_message / append_tool_results
- inject_loaded_skill: mid-loop skill content as a tagged system message in Tier 3

RAG / Graph retrieval injection (`inject_retrieved_context`) is wired up but
no-op in Lite (the RAG layer ships in a later phase). The hook stays so the
Agent Core's call sites don't change when RAG lands.
"""

from __future__ import annotations

from typing import Any

from ..llm._adapter import LLMResponse
from ..session import ConversationView, Message, SessionView
from ..tools._result import ToolResult
from ._static_prefix import LoadedSkill, RepoFacts, StaticPrefixAssembler
from ._system_prompt import load_system_prompt


class ContextBuilder:
    """Stateless assembler. One instance per Agent (or per process).

    The output is always a fresh list — callers should not mutate the list
    in place externally. The Agent Core uses the `append_*` helpers to add
    new messages.
    """

    def __init__(
        self,
        *,
        static_prefix_assembler: StaticPrefixAssembler | None = None,
    ) -> None:
        self._static_prefix_assembler = static_prefix_assembler or StaticPrefixAssembler()

    def build_initial(
        self,
        *,
        session: SessionView,
        conversation: ConversationView,
        new_user_message: str,
        conventions: str | None,
        repo_facts: RepoFacts,
        loaded_skills: list[LoadedSkill] | None = None,
        operation_log_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """First-turn assembly. Returns the OpenAI-format message list.

        If the Conversation already has messages (rare — typically only on
        resume), those are appended after the new user message. The Agent
        Core ensures `build_initial` is only called when starting a new
        Conversation.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_system_prompt()},
            {
                "role": "system",
                "content": self._static_prefix_assembler.assemble(
                    session=session,
                    conventions=conventions,
                    repo_facts=repo_facts,
                    loaded_skills=loaded_skills or [],
                    operation_log_path=operation_log_path,
                ),
            },
        ]
        # Replay any persisted messages first (Conversation resume case).
        messages.extend(m.to_openai_dict() for m in conversation.messages)
        # Then the new user instruction.
        messages.append({"role": "user", "content": new_user_message})
        return messages

    def append_user_message(
        self,
        messages: list[dict[str, Any]],
        content: str,
    ) -> list[dict[str, Any]]:
        messages.append({"role": "user", "content": content})
        return messages

    def append_assistant_message(
        self,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> list[dict[str, Any]]:
        """Add the assistant turn. If the response has tool_calls, those are
        included so the LLM keeps a coherent view on the next turn.
        """
        entry: dict[str, Any] = {"role": "assistant"}
        if response.content is not None:
            entry["content"] = response.content
        else:
            # OpenAI requires `content` even when null; use empty string.
            entry["content"] = ""
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": _json_dump_args(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(entry)
        return messages

    def append_tool_results(
        self,
        messages: list[dict[str, Any]],
        results: list[tuple[str, ToolResult]],
    ) -> list[dict[str, Any]]:
        """Each result is a (tool_call_id, ToolResult) pair. Always appends one
        tool message per tool call — the LLM must see a result for every
        call it made (ADR-0009).
        """
        for tool_call_id, result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": result.tool_name,
                    "content": result.content,
                }
            )
        return messages

    def inject_loaded_skill(
        self,
        messages: list[dict[str, Any]],
        skill: LoadedSkill,
        *,
        loaded_at_turn: int,
    ) -> list[dict[str, Any]]:
        """Insert mid-loop skill content as a tagged system message in Tier 3."""
        tag = f"[SKILL: {skill.name} (loaded mid-conversation at turn {loaded_at_turn})]"
        messages.append({"role": "system", "content": f"{tag}\n{skill.content}"})
        return messages

    def inject_retrieved_context(
        self,
        messages: list[dict[str, Any]],
        retrieved: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """RAG injection. Lite no-op: the call site stays so Standard can fill
        it in. Each retrieved entry is appended as a system message.
        """
        if not retrieved:
            return messages
        body_parts: list[str] = ["[RAG: retrieved snippets]"]
        for entry in retrieved:
            tag = entry.get("provenance", "unknown")
            content = entry.get("content", "")
            body_parts.append(f"--- {tag} ---\n{content}")
        messages.append({"role": "system", "content": "\n\n".join(body_parts)})
        return messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_dump_args(arguments: dict[str, Any]) -> str:
    """OpenAI tool_calls require `function.arguments` to be a JSON STRING,
    not an object. We canonicalize here so the next turn's request is well-formed.
    """
    import json

    return json.dumps(arguments, ensure_ascii=False, default=str)


def messages_to_session_messages(
    raw: list[dict[str, Any]],
) -> list[Message]:
    """Adapter for SessionManager.append_messages.

    The Agent's working messages list uses OpenAI dicts; the SessionManager
    stores Message dataclasses. This helper converts the tail of the working
    list (after build_initial's static prefix system messages) into Message
    objects for persistence.
    """
    return [Message.from_openai_dict(m) for m in raw]
