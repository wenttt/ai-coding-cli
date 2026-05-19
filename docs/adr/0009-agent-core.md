# ADR-0009: Agent Core

## Status

Proposed

## Date

2026-05-19

## Context

Specify the ReAct loop runtime: API, loop logic, tool dispatch, termination conditions, lifecycle hooks, error taxonomy.

## Decision

### Public interface

```python
class Agent:
    """The ReAct runtime. One Agent instance per Conversation."""

    def __init__(
        self,
        *,
        session: Session,
        conversation: Conversation,
        llm: LLMAdapter,
        tools: ToolRegistry,
        context_builder: ContextBuilder,
        skill_loader: SkillLoader,
        guardrail: GuardrailChain,
        observability: EventBus,
        config: AgentConfig,
    ): ...

    async def run(self, user_message: str) -> AgentResult: ...


@dataclass(frozen=True)
class AgentConfig:
    max_turns: int = 20
    max_tokens_per_turn: int = 8000
    max_total_tokens: int = 200_000
    max_parallel_tool_calls: int = 5
    tool_call_timeout_seconds: float = 60.0
    turn_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class AgentResult:
    outcome: Literal["completed", "max_turns_hit", "max_tokens_hit", "fatal_error", "user_abort"]
    final_assistant_message: str | None
    turns_used: int
    tool_calls_made: int
    total_prompt_tokens: int
    total_completion_tokens: int
    error: AgentError | None
```

`Agent` is single-use. One instance per Conversation. The caller (a StageHandler) constructs it, calls `run(user_message)`, consumes the result.

### ReAct loop

```python
async def run(self, user_message: str) -> AgentResult:
    self._emit("agent.started", {...})

    # 1. Build context (System Prompt + Static Prefix + Dynamic Context)
    messages = await self.context_builder.build(
        session=self.session,
        conversation=self.conversation,
        new_user_message=user_message,
    )

    # 2. Run input guardrail on the user message
    await self.guardrail.input_check(user_message, context=self.session)

    # 3. ReAct turns
    for turn_index in range(self.config.max_turns):
        # 3.1 Budget checks
        if self._total_tokens >= self.config.max_total_tokens:
            return self._result_for("max_tokens_hit")

        # 3.2 Pre-turn hook
        await self._emit("turn.starting", {turn_index: turn_index})

        # 3.3 LLM call
        tools_for_this_turn = await self.tools.list_visible_to_agent(self.session)
        response = await self.llm.complete(
            messages=messages,
            tools=tools_for_this_turn,
            timeout=self.config.turn_timeout_seconds,
        )

        # 3.4 Record turn
        turn = Turn(...)
        await self.session_manager.record_turn(self.conversation.id, turn)

        # 3.5 Output guardrail on assistant message
        if response.content:
            await self.guardrail.output_check(response.content, context=self.session)

        # 3.6 Append assistant message to conversation
        messages.append(self._assistant_msg_dict(response))
        await self.session_manager.append_messages(self.conversation.id, [...])

        # 3.7 If no tool calls -> done
        if not response.tool_calls:
            await self._emit("agent.completed", {...})
            return self._result_for("completed", final_message=response.content)

        # 3.8 Dispatch tool calls (with action guardrail)
        tool_results = await self._dispatch_tools(response.tool_calls)

        # 3.9 Append tool results to messages
        for r in tool_results:
            messages.append({"role": "tool", "tool_call_id": r.id, "content": r.content})
        await self.session_manager.append_messages(self.conversation.id, [...])

        await self._emit("turn.ended", {...})

    # 4. Max turns exhausted without a terminal answer
    return self._result_for("max_turns_hit")
```

### Tool dispatch

```python
async def _dispatch_tools(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
    """Run tool calls concurrently up to max_parallel_tool_calls, with action guardrail."""

    # Action guardrail: confirm destructive operations
    confirmed = await self.guardrail.action_check_all(tool_calls, context=self.session)
    if not confirmed.all_allowed:
        return [...refusal entries for refused calls + executable entries for allowed...]

    # Parallel dispatch with bounded concurrency
    sem = asyncio.Semaphore(self.config.max_parallel_tool_calls)

    async def _one(tc: ToolCall) -> ToolResult:
        async with sem:
            await self._emit("tool_call.started", {...})
            try:
                result = await asyncio.wait_for(
                    self.tools.call(tc.name, tc.arguments, ctx=self.session),
                    timeout=self.config.tool_call_timeout_seconds,
                )
            except asyncio.TimeoutError:
                result = ToolResult.timeout(tc.id, tc.name)
            except ToolError as exc:
                result = ToolResult.error(tc.id, tc.name, str(exc))
            await self._emit("tool_call.ended", {...})
            return result

    return await asyncio.gather(*(_one(tc) for tc in confirmed.allowed))
```

Tool calls return one of three result types:

- `success`: the tool returned data. Serialized into the tool message.
- `error`: the tool raised an exception. Error message included in the tool message so the LLM can react.
- `timeout`: the tool exceeded `tool_call_timeout_seconds`. Recorded as timeout in the tool message.
- `refused`: action guardrail vetoed. The tool message says "this action requires human confirmation; ask the user."

The LLM always sees a tool message for every tool call it made — there are no silent omissions.

### Lifecycle events

Emitted on the EventBus (ADR-0015):

| Event | When | Payload |
|---|---|---|
| `agent.started` | Agent.run() entered | session_id, conversation_id, user_message_preview |
| `turn.starting` | before LLM call | turn_index |
| `turn.ended` | after LLM call + tool dispatch (if any) | turn_index, prompt_tokens, completion_tokens, tool_calls_count |
| `tool_call.started` | before tool execution | tool_name, arguments_preview |
| `tool_call.ended` | after tool execution | tool_name, status (success/error/timeout/refused), duration |
| `agent.completed` | terminal assistant message | turns_used, tokens, final_message_preview |
| `agent.halted` | non-terminal exit (max turns, max tokens, fatal error, user abort) | reason, turns_used, error_info |

Subscribers (Memory Store, Dashboard, structured logging) use these events without coupling to Agent internals.

### Termination conditions

The loop exits in one of these states:

| Outcome | Trigger |
|---|---|
| `completed` | LLM produced an assistant message with `tool_calls=[]` |
| `max_turns_hit` | The for-loop ran out of iterations |
| `max_tokens_hit` | Token budget exceeded before turn N |
| `fatal_error` | A `FatalError` was raised at any step |
| `user_abort` | The CLI received SIGINT / the daemon got a stop signal mid-run |

`AgentResult.outcome` carries this. `StageHandler` translates it to `StageResult`:

| AgentResult.outcome | StageResult.outcome |
|---|---|
| completed | completed (handler may inspect the assistant message to fill artifacts) |
| max_turns_hit | failed (with `summary="loop budget exhausted"`; orchestrator decides retry vs escalate) |
| max_tokens_hit | failed |
| fatal_error | failed if RetryableError-derived; escalated if non-recoverable |
| user_abort | failed with `summary="user aborted"`; no retry attempt |

### Error taxonomy

Defined in `foundation/errors.py`:

```python
class AgentError(Exception):
    """Base class for all errors raised by Agent or its dependencies."""

class RetryableError(AgentError):
    """Transient. Retry the same call, possibly after backoff."""

class FatalError(AgentError):
    """Unrecoverable for this Agent invocation. Halt the loop."""

class UserAbort(AgentError):
    """User-initiated termination. Halt without retry."""

# Concrete subclasses
class LLMRateLimitError(RetryableError): ...        # 429 from provider
class LLMTimeoutError(RetryableError): ...
class LLMInvalidResponseError(FatalError): ...      # malformed JSON, schema mismatch
class LLMContextOverflowError(RetryableError): ...  # context window exceeded — Compactor should try again
class ToolError(AgentError):
    """Base for tool-call errors. Wrapped in tool message, NOT raised."""
class GuardrailViolation(FatalError): ...           # input/output guardrail blocked
class ToolRefusedByGuardrail(AgentError):
    """Action guardrail vetoed. Wrapped in tool message, NOT raised."""
```

Error policy:

- **Tool errors do NOT halt the loop.** They are serialized into a tool message; the LLM gets to react. (Exception: `GuardrailViolation` on output halts the loop because the output cannot be safely shown.)
- **LLM transient errors** (`LLMRateLimitError`, `LLMTimeoutError`) are retried up to 3 times within the same turn with exponential backoff. If retries exhaust, the turn fails with `fatal_error`.
- **Context overflow** is special: the Compactor (ADR-0011) is invoked before retrying. If post-compaction the request still overflows, the turn fails.
- **Guardrail violations** are immediately fatal. The agent cannot proceed safely.

### Skill loading mid-loop

The agent may decide to load additional skills during execution (ADR-0012 owns the protocol). The mechanism: the LLM calls a special `load_skill(skill_name)` tool that the Skill Loader fulfills. The result is the skill's content, which is prepended to the next turn's Dynamic Context.

Skill loads count against `max_total_tokens` but not against `max_turns`. The skill content is injected into subsequent turns by the ContextBuilder transparently.

### Streaming (deferred to post-v0.2)

v0.2 uses blocking LLM calls. The LLM Adapter (ADR-0014) supports streaming, but the Agent Core does not consume the stream — it awaits the full response. Streaming integration adds complexity around partial tool-call assembly and partial Guardrail invocation, which is out of v0.2 scope.

### Concurrency model

- Single Agent instance per Conversation, processed sequentially turn-by-turn.
- Tool calls within one turn run concurrently up to `max_parallel_tool_calls`.
- Multiple Agents (across different Conversations / Sessions) may run concurrently in the daemon; the daemon owns the asyncio event loop.
- Tool concurrency safety is the responsibility of individual tools (e.g., file system tools serialize writes to the same path).

### Replay and debugging

Because `Conversation.messages` is persisted and `Turn` records every call, the agent run can be replayed:

```python
async def replay(conversation_id: ConversationId, mock_llm: MockLLMAdapter) -> AgentResult:
    """Re-run an Agent with a MockLLMAdapter primed with the recorded responses.
    Verifies the loop's deterministic parts (turn structure, tool dispatch, error handling)."""
```

Replay is used by integration tests and for debugging production incidents. Tools that have external side effects (creating Jira tickets, opening PRs) must support a "dry-run" mode for replay — covered in ADR-0013.

### Configuration surface

`AgentConfig` is populated from `Config` (ADR-0016). Defaults:

| Field | Default | Override |
|---|---|---|
| `max_turns` | 20 | `AGENT_MAX_TURNS` env var |
| `max_tokens_per_turn` | 8000 | `AGENT_MAX_TOKENS_PER_TURN` |
| `max_total_tokens` | 200,000 | `AGENT_MAX_TOTAL_TOKENS` |
| `max_parallel_tool_calls` | 5 | `AGENT_MAX_PARALLEL_TOOLS` |
| `tool_call_timeout_seconds` | 60 | `AGENT_TOOL_TIMEOUT` |
| `turn_timeout_seconds` | 300 | `AGENT_TURN_TIMEOUT` |

Per-Conversation overrides via `Session.metadata.agent_config_overrides` (used for special-case stages like greenfield design where token budget may need to be higher).

## Consequences

- Stage handlers don't write loop logic. They construct an Agent and call `run()`.
- Tool errors don't halt the loop — the LLM sees them and adapts — which dramatically improves recovery from transient failures.
- Lifecycle events make the loop observable from outside without instrumenting handlers.
- The error taxonomy is shallow enough to reason about (3 categories) but specific enough at the leaves (LLMRateLimitError, GuardrailViolation, etc.).
- Replay capability via deterministic message + turn persistence makes debugging tractable.
- Concurrency is per-turn, bounded; the model stays simple in v0.2.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether `max_total_tokens` should be a hard cutoff or trigger Compactor with a hard fallback | ADR-0011 |
| Q2 | How `load_skill` interacts with `max_total_tokens` (count loaded skill content separately?) | ADR-0012 |
| Q3 | Streaming surface for post-v0.2 — partial tool calls, partial output guardrail | Post-v0.2 design |
| Q4 | When concurrent Conversations within one Session are needed (e.g., parallel sub-task pipelines on the same machine) | Post-v0.2 |

## References

- ADR-0001 System Overview
- ADR-0008 Session + Conversation Model
- ADR-0010 Context Layer (planned)
- ADR-0011 Compactor (planned)
- ADR-0012 Skill Loader (planned)
- ADR-0013 Tool Registry (planned)
- ADR-0014 LLM Adapter (planned)
- ADR-0015 Observability (planned)
- ADR-0017 Error handling taxonomy (planned; this ADR provides the Agent-Core-specific subclasses)
- ADR-0025 Guardrail Layer (planned)

## Reviewers

- [ ] Taven
