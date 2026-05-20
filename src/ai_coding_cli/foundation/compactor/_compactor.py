"""MicroCompact-only Compactor. See ADR-0011 (Lite profile).

Rule-based, no LLM call. The Agent Core invokes `maybe_compact` at the end
of each turn; if no work is needed the messages list is returned unchanged.

The Lite profile skips AutoCompact (LLM-driven summarization) and Tier 2
compaction. Standard layers them on top later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..llm._adapter import LLMAdapter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactorConfig:
    """Tunables for the Lite Compactor. See ADR-0011 §Thresholds."""

    micro_compact_every_n_turns: int = 3
    micro_compact_soft_threshold_tokens: int = 80_000
    preserve_recent_turns: int = 5
    droppable_tool_names: frozenset[str] = frozenset(
        {
            "read_repo_file",
            "list_repo_files",
            "find_relevant_modules",
            "git_log",
            "git_diff",
        }
    )
    droppable_min_content_bytes: int = 4_096


@dataclass
class CompactionResult:
    """Telemetry from a compaction pass. Returned to the Agent Core."""

    triggered: bool
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    tool_messages_dropped: int = 0
    placeholder_inserted: bool = False
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compactor
# ---------------------------------------------------------------------------


class Compactor:
    """Lite MicroCompact. Deterministic, fast, in-process.

    Caller invokes `maybe_compact(messages, turn_index)` at end-of-turn.
    The return value tells whether anything changed; the messages list is
    returned (a new list — the original is not mutated in place).
    """

    def __init__(self, llm: LLMAdapter, config: CompactorConfig | None = None) -> None:
        self._llm = llm
        self._config = config or CompactorConfig()

    @property
    def config(self) -> CompactorConfig:
        return self._config

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return self._llm.count_tokens(messages)

    def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        turn_index: int,
        static_prefix_msg_count: int = 2,
    ) -> tuple[list[dict[str, Any]], CompactionResult]:
        """Run MicroCompact if triggered. Returns (new_messages, result).

        `static_prefix_msg_count` is the number of leading system messages
        that constitute Tier 1 + Tier 2 and must never be touched. Default
        2 matches the ContextBuilder.build_initial layout.
        """
        cfg = self._config
        tokens_before = self.count_tokens(messages)
        size_trigger = tokens_before > cfg.micro_compact_soft_threshold_tokens
        cadence_trigger = (
            cfg.micro_compact_every_n_turns > 0
            and turn_index > 0
            and turn_index % cfg.micro_compact_every_n_turns == 0
        )
        if not (size_trigger or cadence_trigger):
            return messages, CompactionResult(
                triggered=False,
                messages_before=len(messages),
                messages_after=len(messages),
                tokens_before=tokens_before,
                tokens_after=tokens_before,
            )

        new_messages, dropped_count, notes = _micro_compact(
            messages,
            cfg=cfg,
            static_prefix_msg_count=static_prefix_msg_count,
        )
        tokens_after = self.count_tokens(new_messages)
        return new_messages, CompactionResult(
            triggered=True,
            messages_before=len(messages),
            messages_after=len(new_messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tool_messages_dropped=dropped_count,
            placeholder_inserted=dropped_count > 0,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# MicroCompact algorithm
# ---------------------------------------------------------------------------


def _micro_compact(
    messages: list[dict[str, Any]],
    *,
    cfg: CompactorConfig,
    static_prefix_msg_count: int,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Return (new_messages, tool_messages_dropped, notes)."""
    notes: list[str] = []
    if len(messages) <= static_prefix_msg_count + 1:
        notes.append("nothing to compact (no dynamic messages yet)")
        return list(messages), 0, notes

    # 1. Always preserve: leading static prefix + first user message + last
    #    `preserve_recent_turns` assistant/tool message pairs.
    static_prefix = messages[:static_prefix_msg_count]
    dynamic = messages[static_prefix_msg_count:]

    first_user_idx = _find_first_user_index(dynamic)
    if first_user_idx is None:
        notes.append("no user message found in dynamic; skipping")
        return list(messages), 0, notes

    # 2. Identify which assistant messages anchor "recent turns": last N
    #    assistant messages. Everything after the earliest of those (and
    #    their paired tool messages + any following user replies) is
    #    preserved verbatim.
    assistant_indices = [
        i for i, m in enumerate(dynamic) if m.get("role") == "assistant"
    ]
    if len(assistant_indices) <= cfg.preserve_recent_turns:
        notes.append(
            "fewer than preserve_recent_turns assistants present; skipping"
        )
        return list(messages), 0, notes

    boundary_idx = assistant_indices[-cfg.preserve_recent_turns]

    # 3. The "old zone" is dynamic[first_user_idx+1 : boundary_idx]
    #    (we keep dynamic[:first_user_idx+1] as the original task + we keep
    #    dynamic[boundary_idx:] as recent turns).
    head = dynamic[: first_user_idx + 1]
    old_zone = dynamic[first_user_idx + 1 : boundary_idx]
    tail = dynamic[boundary_idx:]

    rebuilt_old: list[dict[str, Any]] = []
    dropped = 0
    for msg in old_zone:
        if _should_drop(msg, cfg):
            dropped += 1
            continue
        rebuilt_old.append(msg)

    if dropped > 0:
        placeholder = {
            "role": "system",
            "content": (
                f"[COMPACTED: dropped {dropped} verbose tool result"
                f"{'s' if dropped != 1 else ''} from earlier turns. "
                "Re-derive via tool calls if needed.]"
            ),
        }
        rebuilt_old.append(placeholder)
        notes.append(f"dropped {dropped} large tool result(s) from old zone")

    return static_prefix + head + rebuilt_old + tail, dropped, notes


def _find_first_user_index(dynamic: list[dict[str, Any]]) -> int | None:
    for i, m in enumerate(dynamic):
        if m.get("role") == "user":
            return i
    return None


def _should_drop(msg: dict[str, Any], cfg: CompactorConfig) -> bool:
    """A tool message is droppable when:
    - role == tool
    - the tool name is in the droppable allowlist
    - the content is large (>= droppable_min_content_bytes)
    - the content is NOT an error/timeout marker (those carry signal)
    """
    if msg.get("role") != "tool":
        return False
    name = msg.get("name")
    if name not in cfg.droppable_tool_names:
        return False
    content = msg.get("content") or ""
    if not isinstance(content, str):
        return False
    if len(content.encode("utf-8", errors="replace")) < cfg.droppable_min_content_bytes:
        return False
    if content.startswith(("[ERROR]", "[TIMEOUT]", "[REFUSED]")):
        return False
    return True
