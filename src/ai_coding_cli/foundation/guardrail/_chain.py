"""Guardrail chain (Lite: rule-based only). See ADR-0025."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ..config import GuardrailConfig
from ..llm._adapter import ToolCall
from ..tools import SideEffectClass, Tool, ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputDecision:
    outcome: Literal["allow", "block"]
    detected_signals: list[str] = field(default_factory=list)
    user_message: str | None = None


@dataclass(frozen=True)
class OutputDecision:
    outcome: Literal["allow", "block", "rewritten"]
    final_content: str
    detected_signals: list[str] = field(default_factory=list)
    user_message: str | None = None


@dataclass(frozen=True)
class RefusedCall:
    tool_call: ToolCall
    reason: str


@dataclass(frozen=True)
class PendingCall:
    tool_call: ToolCall
    reason: str


@dataclass(frozen=True)
class ActionDecision:
    allowed: list[ToolCall]
    refused: list[RefusedCall]
    awaiting_confirmation: list[PendingCall]

    @property
    def all_allowed(self) -> bool:
        return not self.refused and not self.awaiting_confirmation


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GuardrailChain(Protocol):
    async def input_check(
        self,
        text: str,
        *,
        kind: Literal["user_message", "tool_result", "rag_snippet"],
    ) -> InputDecision: ...

    async def output_check(self, content: str) -> OutputDecision: ...

    async def action_check_all(
        self, tool_calls: list[ToolCall]
    ) -> ActionDecision: ...


# ---------------------------------------------------------------------------
# Detection rules (rule-based; Lite has no LLM check)
# ---------------------------------------------------------------------------


_INSTRUCTION_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|the\s+above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\s*:\s*", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+(?:an?\s+)?", re.IGNORECASE),
    re.compile(r"\bforget\s+(the\s+above|everything|all)\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(the\s+above|prior|previous)\b", re.IGNORECASE),
)

_SYSTEM_IMPERSONATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:System|<\|im_start\|>system|<\|system\|>|\[\[SYSTEM\]\])", re.IGNORECASE | re.MULTILINE),
)

# Secret shapes — see ADR-0025 §Input + Output. False positives are acceptable
# for an output-redaction context (the agent can resubmit without the secret).
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key[\"'\s:=]+[A-Za-z0-9/+=]{40}")),
    ("github_classic_pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


# ---------------------------------------------------------------------------
# Null implementation (for tests + scripts where guardrails are disabled)
# ---------------------------------------------------------------------------


class NullGuardrailChain:
    """No-op chain. Use in tests or when GuardrailConfig disables a layer."""

    async def input_check(
        self,
        text: str,  # noqa: ARG002
        *,
        kind: Literal["user_message", "tool_result", "rag_snippet"],  # noqa: ARG002
    ) -> InputDecision:
        return InputDecision(outcome="allow")

    async def output_check(self, content: str) -> OutputDecision:
        return OutputDecision(outcome="allow", final_content=content)

    async def action_check_all(self, tool_calls: list[ToolCall]) -> ActionDecision:
        return ActionDecision(allowed=list(tool_calls), refused=[], awaiting_confirmation=[])


# ---------------------------------------------------------------------------
# Lite chain (rule-based)
# ---------------------------------------------------------------------------


class LiteGuardrailChain:
    """Lite chain: rule-based input + output + action checks.

    Lite scope per ADR-0030:
    - Input: regex-only (no LLM-based second pass).
    - Output: secret redaction (rewrite), no PII detection.
    - Action: SideEffectClass-driven matrix (always headless-auto-refuse for
      `awaiting_confirmation` in Lite — the Dashboard confirmation flow lands
      in a later phase).
    """

    def __init__(
        self,
        *,
        config: GuardrailConfig,
        tool_registry: ToolRegistry,
        confirmation_handler: "ConfirmationHandler | None" = None,
    ) -> None:
        self._config = config
        self._tools = tool_registry
        self._confirmation_handler = confirmation_handler

    # -----------------------------------------------------------------
    # Input
    # -----------------------------------------------------------------

    async def input_check(
        self,
        text: str,
        *,
        kind: Literal["user_message", "tool_result", "rag_snippet"],
    ) -> InputDecision:
        if not self._config.input_check_enabled:
            return InputDecision(outcome="allow")

        signals = _detect_input_signals(text)
        if not signals:
            return InputDecision(outcome="allow")

        threshold = _threshold_for_kind(self._config, kind)
        score = _score_input_signals(signals)
        if score >= threshold:
            return InputDecision(
                outcome="block",
                detected_signals=signals,
                user_message=(
                    f"Input guardrail blocked {kind!r} content "
                    f"(detected: {', '.join(signals)})."
                ),
            )
        return InputDecision(outcome="allow", detected_signals=signals)

    # -----------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------

    async def output_check(self, content: str) -> OutputDecision:
        if not self._config.output_check_enabled:
            return OutputDecision(outcome="allow", final_content=content)

        # Secret leak: if found, rewrite (preferred per ADR-0025 §Output rules)
        # OR block if `output_secret_block=True`.
        redacted, signals = _redact_secrets(content)
        if signals:
            if self._config.output_secret_block:
                return OutputDecision(
                    outcome="block",
                    final_content=content,
                    detected_signals=signals,
                    user_message=(
                        f"Output guardrail blocked assistant content carrying "
                        f"{', '.join(signals)}."
                    ),
                )
            return OutputDecision(
                outcome="rewritten",
                final_content=redacted,
                detected_signals=signals,
            )
        return OutputDecision(outcome="allow", final_content=content)

    # -----------------------------------------------------------------
    # Action
    # -----------------------------------------------------------------

    async def action_check_all(
        self, tool_calls: list[ToolCall]
    ) -> ActionDecision:
        allowed: list[ToolCall] = []
        refused: list[RefusedCall] = []
        pending: list[PendingCall] = []
        mode = self._config.action_confirmation_mode

        for tc in tool_calls:
            tool = self._safe_get_tool(tc.name)
            if tool is None:
                # Unknown tool — let the ToolRegistry handle the error path.
                allowed.append(tc)
                continue
            decision = _classify_tool(
                tool=tool,
                mode=mode,
            )
            if decision == "allow":
                allowed.append(tc)
            elif decision == "awaiting_confirmation":
                pending.append(
                    PendingCall(
                        tool_call=tc,
                        reason=(
                            f"{tool.side_effects.value} action requires confirmation "
                            f"(mode={mode!r})"
                        ),
                    )
                )

        # Lite: resolve pending via the confirmation handler if one is wired up;
        # otherwise headless auto-refuse per ADR-0025 §Confirmation flow.
        if pending and self._confirmation_handler is None:
            if self._config.action_headless:
                refused.extend(
                    RefusedCall(
                        tool_call=p.tool_call,
                        reason="headless: confirmation auto-refused",
                    )
                    for p in pending
                )
                pending = []
            else:
                # Default Lite behaviour: auto-refuse (Standard wires the
                # CLI / Dashboard prompt here).
                refused.extend(
                    RefusedCall(
                        tool_call=p.tool_call,
                        reason="no confirmation channel configured (headless default)",
                    )
                    for p in pending
                )
                pending = []
        elif pending:
            # Hand pending to the configured handler synchronously.
            resolved_allowed, resolved_refused = await self._confirmation_handler.resolve(
                pending
            )
            allowed.extend(resolved_allowed)
            refused.extend(resolved_refused)
            pending = []

        return ActionDecision(
            allowed=allowed,
            refused=refused,
            awaiting_confirmation=pending,
        )

    def _safe_get_tool(self, name: str) -> Tool | None:
        if not self._tools.has(name):
            return None
        return self._tools.get(name)


# ---------------------------------------------------------------------------
# Confirmation handler protocol (Lite no-op; Standard wires CLI + Dashboard)
# ---------------------------------------------------------------------------


@runtime_checkable
class ConfirmationHandler(Protocol):
    async def resolve(
        self, pending: list[PendingCall]
    ) -> tuple[list[ToolCall], list[RefusedCall]]: ...


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_input_signals(text: str) -> list[str]:
    signals: list[str] = []
    for pattern in _INSTRUCTION_INJECTION_PATTERNS:
        if pattern.search(text):
            signals.append("instruction_injection")
            break
    for pattern in _SYSTEM_IMPERSONATION_PATTERNS:
        if pattern.search(text):
            signals.append("system_impersonation")
            break
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            signals.append(f"credential_leak:{name}")
            break  # one is enough; we deduplicate the rest
    return signals


def _score_input_signals(signals: list[str]) -> float:
    """Each independent signal contributes; total capped at 1.0."""
    base = 0.0
    if "system_impersonation" in signals:
        base += 0.7
    if "instruction_injection" in signals:
        base += 0.7
    if any(s.startswith("credential_leak") for s in signals):
        base += 0.3
    return min(1.0, base)


def _threshold_for_kind(
    cfg: GuardrailConfig,
    kind: Literal["user_message", "tool_result", "rag_snippet"],
) -> float:
    if kind == "tool_result":
        return cfg.prompt_injection_threshold_tool_result
    if kind == "rag_snippet":
        return cfg.prompt_injection_threshold_rag
    return cfg.prompt_injection_threshold


def _redact_secrets(content: str) -> tuple[str, list[str]]:
    signals: list[str] = []
    redacted = content
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(redacted):
            signals.append(f"secret:{name}")
            redacted = pattern.sub(f"<redacted:{name}>", redacted)
    return redacted, signals


# ---------------------------------------------------------------------------
# Action classification (the matrix from ADR-0025 §Action Guardrail)
# ---------------------------------------------------------------------------


def _classify_tool(
    *,
    tool: Tool,
    mode: Literal["never", "destructive_only", "always"],
) -> Literal["allow", "awaiting_confirmation"]:
    if tool.side_effects in (SideEffectClass.READ_ONLY, SideEffectClass.EXTERNAL_READ):
        return "allow"

    if tool.side_effects == SideEffectClass.DESTRUCTIVE:
        if mode == "never":
            return "allow"
        return "awaiting_confirmation"

    # LOCAL_WRITE + EXTERNAL_WRITE
    if not tool.requires_confirmation:
        return "allow"
    if mode == "always":
        return "awaiting_confirmation"
    return "allow"
