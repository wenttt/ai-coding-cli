"""ReAct loop. See ADR-0009 + ADR-0030.

Lite scope:
- Sequential turns, parallel tool dispatch with bounded concurrency
- Retries on LLMRateLimitError / LLMTimeoutError with exponential backoff
- AutoCompact via Compactor.maybe_compact() at end of turn
- LLMContextOverflowError -> force-compact + one retry
- Guardrail integration: Input check on user_message + tool results, Output
  check on assistant content, Action check on tool_calls
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..compactor import Compactor
from ..config import Config
from ..context import ContextBuilder, LoadedSkill, RepoFacts
from ..errors import (
    AgentError,
    FatalError,
    GuardrailInputBlocked,
    GuardrailOutputBlocked,
    LLMContextOverflowError,
    LLMRateLimitError,
    LLMTimeoutError,
    UserAbort,
)
from ..guardrail import GuardrailChain, NullGuardrailChain
from ..llm._adapter import LLMAdapter, LLMResponse, ToolCall
from ..session import ConversationView, Message, SessionManager, SessionView, TurnRecord
from ..tools import (
    SideEffectClass,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
)
from ._result import AgentOutcome, AgentResult

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Agent:
    """Single-use ReAct runtime. One instance per Conversation. See ADR-0009."""

    def __init__(
        self,
        *,
        session: SessionView,
        conversation: ConversationView,
        llm: LLMAdapter,
        tools: ToolRegistry,
        context_builder: ContextBuilder,
        compactor: Compactor,
        session_manager: SessionManager,
        config: Config,
        repo_facts: RepoFacts,
        conventions: str | None = None,
        loaded_skills: list[LoadedSkill] | None = None,
        operation_log_path: str | None = None,
        dry_run: bool = False,
        guardrail: GuardrailChain | None = None,
    ) -> None:
        self._session = session
        self._conversation = conversation
        self._llm = llm
        self._tools = tools
        self._context_builder = context_builder
        self._compactor = compactor
        self._session_manager = session_manager
        self._config = config
        self._agent_cfg = config.agent
        self._repo_facts = repo_facts
        self._conventions = conventions
        self._loaded_skills = loaded_skills or []
        self._operation_log_path = operation_log_path
        self._dry_run = dry_run
        self._guardrail: GuardrailChain = guardrail or NullGuardrailChain()

        # Running aggregates
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_cache_hit_tokens = 0
        self._tool_calls_made = 0
        self._compaction_passes = 0
        self._compaction_tokens_saved = 0
        self._notes: list[str] = []
        self._used = False

    async def run(self, user_message: str) -> AgentResult:
        """Execute the ReAct loop. Returns when the LLM produces a
        no-tool-call response, hits a budget, or fails.

        This method must be called at most once per Agent instance.
        """
        if self._used:
            raise RuntimeError("Agent is single-use; instantiate a new one per run.")
        self._used = True

        cfg = self._agent_cfg
        logger.info(
            "agent.started",
            extra={
                "session_id": self._session.id,
                "conversation_id": self._conversation.id,
                "jira_key": self._session.jira_key,
                "stage": self._conversation.stage,
            },
        )

        # Guardrail: input check on the user message before anything else.
        try:
            input_decision = await self._guardrail.input_check(
                user_message, kind="user_message"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent.guardrail_input_error: %s", exc)
            input_decision = None
        if input_decision is not None and input_decision.outcome == "block":
            blocked = GuardrailInputBlocked(
                input_decision.user_message or "Input guardrail blocked user message.",
                detected_signals=input_decision.detected_signals,
            )
            logger.warning("agent.input_blocked: %s", blocked)
            return self._result(
                AgentOutcome.FATAL_ERROR, final_message=None, error=blocked
            )

        # Build the initial message list (Tier 1 + Tier 2 + first user message).
        messages = self._context_builder.build_initial(
            session=self._session,
            conversation=self._conversation,
            new_user_message=user_message,
            conventions=self._conventions,
            repo_facts=self._repo_facts,
            loaded_skills=self._loaded_skills,
            operation_log_path=self._operation_log_path,
        )

        # Persist the new user message so the Conversation row stays in sync.
        await self._session_manager.append_messages(
            self._conversation.id,
            [Message(role="user", content=user_message)],
        )

        try:
            for turn_index in range(cfg.max_turns):
                # Budget check before the LLM call.
                if (
                    self._total_prompt_tokens + self._total_completion_tokens
                    >= cfg.max_total_tokens
                ):
                    return self._result(AgentOutcome.MAX_TOKENS_HIT, final_message=None)

                # ---------------- LLM call (with retries) ----------------
                tools_schema = self._tools.schemas_for_llm()
                response = await self._llm_with_retry(
                    messages=messages,
                    tools=tools_schema,
                    turn_index=turn_index,
                )

                # Record the turn before we mutate messages further.
                turn_started = _utcnow()

                # Aggregate token counts.
                self._total_prompt_tokens += response.prompt_tokens
                self._total_completion_tokens += response.completion_tokens
                self._total_cache_hit_tokens += getattr(response, "cache_hit_tokens", 0) or 0

                # Guardrail: output check on assistant content. May rewrite or block.
                output_decision = await self._guardrail.output_check(response.content or "")
                if output_decision.outcome == "block":
                    blocked = GuardrailOutputBlocked(
                        output_decision.user_message
                        or "Output guardrail blocked assistant content.",
                        detected_signals=output_decision.detected_signals,
                    )
                    logger.warning("agent.output_blocked: %s", blocked)
                    return self._result(
                        AgentOutcome.FATAL_ERROR, final_message=None, error=blocked
                    )
                if output_decision.outcome == "rewritten":
                    response = LLMResponse(
                        content=output_decision.final_content,
                        tool_calls=response.tool_calls,
                        finish_reason=response.finish_reason,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        total_tokens=response.total_tokens,
                        cache_hit_tokens=getattr(response, "cache_hit_tokens", 0) or 0,
                        raw_provider_response=response.raw_provider_response,
                    )
                    self._notes.append(
                        f"output guardrail rewrote turn {turn_index} content "
                        f"(signals: {', '.join(output_decision.detected_signals)})"
                    )

                # Append the assistant message to both working list + persisted log.
                self._context_builder.append_assistant_message(messages, response)
                await self._session_manager.append_messages(
                    self._conversation.id,
                    [_assistant_message_for_persistence(response)],
                )

                tool_calls = response.tool_calls or []

                # Persist the Turn statistics row.
                await self._session_manager.record_turn(
                    TurnRecord(
                        conversation_id=self._conversation.id,
                        turn_index=turn_index,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        cache_hit_tokens=getattr(response, "cache_hit_tokens", 0) or 0,
                        tool_calls=[
                            {"id": tc.id, "name": tc.name} for tc in tool_calls
                        ],
                        finish_reason=response.finish_reason or "stop",
                        started_at=turn_started,
                        ended_at=_utcnow(),
                        latency_seconds=0.0,
                    )
                )

                # ---------------- Terminal: no tool calls ----------------
                if not tool_calls:
                    return self._result(
                        AgentOutcome.COMPLETED,
                        final_message=response.content or "",
                    )

                # ---------------- Action guardrail ----------------
                action_decision = await self._guardrail.action_check_all(tool_calls)
                # Synthesize ToolResults for refused calls; dispatch only allowed ones.
                refusal_results: dict[str, ToolResult] = {
                    rc.tool_call.id: ToolResult.refused(
                        tool_name=rc.tool_call.name,
                        invocation_id=uuid4().hex,
                        reason=rc.reason,
                    )
                    for rc in action_decision.refused
                }
                allowed_calls = action_decision.allowed
                self._tool_calls_made += len(allowed_calls) + len(action_decision.refused)
                dispatched = (
                    await self._dispatch_tools(allowed_calls) if allowed_calls else []
                )
                # Map call_id -> ToolResult, preserving original order.
                dispatched_by_id = dict(
                    zip([tc.id for tc in allowed_calls], dispatched)
                )
                ordered_results: list[ToolResult] = []
                for tc in tool_calls:
                    if tc.id in refusal_results:
                        ordered_results.append(refusal_results[tc.id])
                    else:
                        ordered_results.append(dispatched_by_id[tc.id])

                # Guardrail: input check on each tool result before they enter the LLM context.
                for r in ordered_results:
                    if r.status != ToolResultStatus.SUCCESS:
                        continue  # Error/timeout/refused markers are not user-controlled input.
                    decision = await self._guardrail.input_check(
                        r.content or "", kind="tool_result"
                    )
                    if decision.outcome == "block":
                        blocked = GuardrailInputBlocked(
                            decision.user_message
                            or f"Input guardrail blocked tool result for {r.tool_name!r}.",
                            detected_signals=decision.detected_signals,
                        )
                        logger.warning("agent.tool_result_blocked: %s", blocked)
                        return self._result(
                            AgentOutcome.FATAL_ERROR,
                            final_message=None,
                            error=blocked,
                        )

                # Append tool results to both working list + persisted log.
                paired = list(zip([tc.id for tc in tool_calls], ordered_results))
                self._context_builder.append_tool_results(messages, paired)
                await self._session_manager.append_messages(
                    self._conversation.id,
                    [
                        Message(
                            role="tool",
                            content=r.content,
                            name=r.tool_name,
                            tool_call_id=tc_id,
                        )
                        for tc_id, r in paired
                    ],
                )

                # ---------------- MicroCompact (end-of-turn) ----------------
                messages, compaction = self._compactor.maybe_compact(
                    messages, turn_index=turn_index + 1
                )
                if compaction.triggered:
                    self._compaction_passes += 1
                    self._compaction_tokens_saved += (
                        compaction.tokens_before - compaction.tokens_after
                    )
                    if compaction.placeholder_inserted:
                        # Persist the rewritten message tail so a resume sees
                        # the same compacted state.
                        await self._session_manager.overwrite_messages(
                            self._conversation.id,
                            _messages_to_persistence_objects(
                                messages,
                                static_prefix_msg_count=2,
                            ),
                        )
                    self._notes.extend(compaction.notes)

            # Loop fell through without a terminal answer.
            return self._result(AgentOutcome.MAX_TURNS_HIT, final_message=None)

        except UserAbort as exc:
            logger.warning("agent.user_abort: %s", exc)
            return self._result(
                AgentOutcome.USER_ABORT,
                final_message=None,
                error=exc,
            )
        except FatalError as exc:
            logger.exception("agent.fatal_error: %s", exc)
            return self._result(
                AgentOutcome.FATAL_ERROR,
                final_message=None,
                error=exc,
            )

    # -----------------------------------------------------------------
    # LLM call with retries + context-overflow handling
    # -----------------------------------------------------------------

    async def _llm_with_retry(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        turn_index: int,
    ) -> LLMResponse:
        cfg = self._config.llm
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= cfg.rate_limit_retry_max:
            try:
                return await self._llm.complete(
                    messages=messages,
                    tools=tools or None,
                    timeout_seconds=self._agent_cfg.turn_timeout_seconds,
                )
            except LLMContextOverflowError as exc:
                # Force-compact then retry once.
                logger.warning(
                    "agent.context_overflow at turn %d; forcing compaction.",
                    turn_index,
                )
                messages_new, compaction = self._compactor.maybe_compact(
                    messages, turn_index=turn_index, static_prefix_msg_count=2
                )
                if not compaction.placeholder_inserted:
                    # Nothing dropped; can't recover.
                    raise exc
                # Mutate caller's list in place so the loop sees the new state.
                messages.clear()
                messages.extend(messages_new)
                self._compaction_passes += 1
                self._compaction_tokens_saved += (
                    compaction.tokens_before - compaction.tokens_after
                )
                self._notes.append(
                    "force-compact after LLMContextOverflowError"
                )
                attempt += 1
                last_exc = exc
                continue
            except (LLMRateLimitError, LLMTimeoutError) as exc:
                attempt += 1
                last_exc = exc
                if attempt > cfg.rate_limit_retry_max:
                    raise
                backoff = cfg.rate_limit_retry_base_seconds * (2 ** (attempt - 1))
                if isinstance(exc, LLMRateLimitError) and exc.retry_after_seconds:
                    backoff = max(backoff, exc.retry_after_seconds)
                logger.warning(
                    "agent.retryable_llm_error attempt=%d backoff=%.2fs error=%s",
                    attempt,
                    backoff,
                    type(exc).__name__,
                )
                await asyncio.sleep(backoff)
        # Exhausted.
        assert last_exc is not None
        raise last_exc

    # -----------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------

    async def _dispatch_tools(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """Run tool calls with bounded concurrency. One ToolResult per call,
        in the same order.
        """
        sem = asyncio.Semaphore(self._agent_cfg.max_parallel_tool_calls)

        ctx_template = ToolContext(
            config=self._config,
            session_id=None,
            conversation_id=None,
            invocation_id=uuid4().hex,
            dry_run=self._dry_run,
        )

        async def _run_one(tc: ToolCall) -> ToolResult:
            async with sem:
                # Lite has no Action Guardrail; refusal for DESTRUCTIVE calls
                # falls to the tool itself or the upcoming Week-4 layer.
                if self._dry_run and self._is_external_write(tc):
                    return ToolResult.refused(
                        tool_name=tc.name,
                        invocation_id=uuid4().hex,
                        reason="dry_run: external writes are skipped",
                    )
                started = time.monotonic()
                try:
                    return await self._tools.call(tc.name, tc.arguments, ctx_template)
                except Exception as exc:  # noqa: BLE001
                    # ToolRegistry already wraps tool errors; this branch
                    # catches misc dispatch failures (e.g., serialization).
                    return ToolResult.error(
                        tool_name=tc.name,
                        invocation_id=uuid4().hex,
                        message=f"{type(exc).__name__}: {exc}",
                        duration_seconds=time.monotonic() - started,
                    )

        return await asyncio.gather(*(_run_one(tc) for tc in tool_calls))

    def _is_external_write(self, tc: ToolCall) -> bool:
        if not self._tools.has(tc.name):
            return False
        return self._tools.get(tc.name).side_effects in (
            SideEffectClass.EXTERNAL_WRITE,
            SideEffectClass.DESTRUCTIVE,
        )

    # -----------------------------------------------------------------
    # Result construction
    # -----------------------------------------------------------------

    def _result(
        self,
        outcome: AgentOutcome,
        *,
        final_message: str | None,
        error: AgentError | None = None,
    ) -> AgentResult:
        return AgentResult(
            outcome=outcome,
            final_assistant_message=final_message,
            turns_used=self._compaction_passes_to_turns(),
            tool_calls_made=self._tool_calls_made,
            total_prompt_tokens=self._total_prompt_tokens,
            total_completion_tokens=self._total_completion_tokens,
            total_cache_hit_tokens=self._total_cache_hit_tokens,
            error=error,
            compaction_passes=self._compaction_passes,
            compaction_tokens_saved=self._compaction_tokens_saved,
            notes=list(self._notes),
        )

    def _compaction_passes_to_turns(self) -> int:
        """Best-effort: SessionManager tracks the true count via record_turn;
        for the result we report the local counter. Standard wires through.
        """
        return self._tool_calls_made_to_turn_estimate()

    def _tool_calls_made_to_turn_estimate(self) -> int:
        # Tool-call count is a lower bound on turn count; the Agent doesn't
        # track turns directly because record_turn is the source of truth.
        # We expose it through tool_calls_made in the result instead.
        return self._tool_calls_made


# ---------------------------------------------------------------------------
# Adapters between LLMResponse / OpenAI dict and Message dataclass
# ---------------------------------------------------------------------------


def _assistant_message_for_persistence(response: LLMResponse) -> Message:
    tool_calls_payload: list[dict[str, Any]] | None = None
    if response.tool_calls:
        tool_calls_payload = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False, default=str),
                },
            }
            for tc in response.tool_calls
        ]
    return Message(
        role="assistant",
        content=response.content or "",
        tool_calls=tool_calls_payload,
    )


def _messages_to_persistence_objects(
    messages: list[dict[str, Any]],
    *,
    static_prefix_msg_count: int,
) -> list[Message]:
    """Convert the tail (after Tier 1 + Tier 2 system messages) for persistence.

    The Conversation row only stores Tier 3 (dynamic) content; Tier 1/2 are
    reconstructed by ContextBuilder on resume.
    """
    tail = messages[static_prefix_msg_count:]
    out: list[Message] = []
    for m in tail:
        out.append(Message.from_openai_dict(m))
    return out
