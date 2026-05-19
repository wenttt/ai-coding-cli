# ADR-0011: Compactor (MicroCompact + AutoCompact)

## Status

Proposed

## Date

2026-05-19

## Context

Specify the two-tier compaction subsystem that keeps the Dynamic Context (Tier 3 from ADR-0010) within the LLM's context window: trigger conditions, algorithms, the hook into Memory write governance, and what content is preserved vs replaced with summaries.

## Decision

### Two compaction modes

| Mode | Trigger | Scope | Frequency | Cost |
|---|---|---|---|---|
| **MicroCompact** | Every N turns OR Dynamic size > soft threshold | Tier 3 only; surgical | Often (every 3-5 turns) | Low (no LLM call; rule-based) |
| **AutoCompact** | Dynamic size > hard threshold OR `LLMContextOverflowError` raised | Tier 3 + (rarely) Tier 2 | Rare | Higher (uses LLM to summarize) |

Both modes write extracted facts to Working Memory before discarding content (memory write hook).

### Thresholds

```python
class CompactorConfig(BaseModel):
    micro_compact_every_n_turns: int = 3
    micro_compact_soft_threshold_tokens: int = 80_000   # Tier 3 size
    auto_compact_hard_threshold_tokens: int = 130_000   # Tier 3 size
    preserve_recent_turns: int = 5                        # always keep the last N turns intact
    preserve_first_user_message: bool = True              # the initial task instruction
    preserve_assistant_summary_msg: bool = True           # a message tagged [COMPACTED_SUMMARY]
```

Defaults sized so Tier 3 stays comfortably under the 150k allocation from ADR-0010, with a ~20k buffer before the hard limit.

### MicroCompact

**Trigger**: at the end of a turn, if `turn_index % micro_compact_every_n_turns == 0` OR `dynamic_token_count > micro_compact_soft_threshold_tokens`.

**Algorithm** (no LLM call):

```python
async def micro_compact(messages: list[Message], cfg: CompactorConfig) -> CompactionResult:
    # 1. Identify the "old" zone: everything except the last `preserve_recent_turns` turns
    # 2. Within the old zone, apply rule-based dropping:
    #    a. Drop tool messages whose result was successful AND whose content is large
    #       (> 4 KB) AND whose tool name is in {read_repo_file, find_relevant_modules,
    #       list_directory, git_log}. These are easy to re-derive if needed.
    #    b. Drop assistant tool_calls + paired tool messages where the LLM's reasoning
    #       between them did not reference the result (heuristic: subsequent assistant
    #       content doesn't quote a substring from the result).
    #    c. Replace dropped tool messages with a placeholder system message
    #       "[COMPACTED: dropped N tool result(s)]" so the LLM sees there was activity.
    # 3. Extract facts from dropped messages BEFORE discarding
    #    (Memory governance hook; see Memory extraction below)
    # 4. Return the new message list + telemetry (chars dropped, tokens dropped)
```

MicroCompact is deterministic, fast (< 100ms for typical conversations), and reversible at the message level — the dropped tool results are recoverable from PostgreSQL since every Turn is persisted (ADR-0008).

### AutoCompact

**Trigger**: at the start of a turn, if `dynamic_token_count > auto_compact_hard_threshold_tokens`. Also triggered by the Agent Core when `LLMContextOverflowError` is caught (ADR-0009).

**Algorithm** (uses LLM):

```python
async def auto_compact(messages: list[Message], cfg: CompactorConfig, llm: LLMAdapter) -> CompactionResult:
    # 1. Run MicroCompact first (cheap; may be sufficient on its own)
    micro_result = await micro_compact(messages, cfg)
    if micro_result.final_tokens <= cfg.auto_compact_hard_threshold_tokens:
        return micro_result

    # 2. Partition: PRESERVE + COMPACT_TARGET
    #    PRESERVE = system messages, the first user message, the last preserve_recent_turns turns
    #    COMPACT_TARGET = everything else in between

    # 3. Summarize COMPACT_TARGET via a dedicated LLM call
    summary = await llm.summarize_for_compaction(
        messages=COMPACT_TARGET,
        instructions=COMPACTION_INSTRUCTIONS,    # detailed prompt; see below
    )

    # 4. Extract structured facts from the summary
    #    (Memory governance hook; written to Working Memory)

    # 5. Replace COMPACT_TARGET with a single system message:
    #    {"role": "system", "content": "[COMPACTED_SUMMARY]\n\n{summary}"}
    return CompactionResult(messages=PRESERVE_FRONT + [SUMMARY_MSG] + PRESERVE_BACK, ...)
```

`COMPACTION_INSTRUCTIONS` (the prompt used to summarize) emphasizes:

- Preserve all decisions made (file paths chosen, schema field names, error codes, library versions, env var names).
- Preserve all open questions / unresolved items.
- Preserve all tool calls' artifacts (URLs, IDs, file paths).
- Drop verbose reasoning that didn't lead to a decision.
- Drop tool results that the LLM didn't reference downstream.
- Output is a Markdown summary; sections: `Decisions`, `Artifacts`, `Open Questions`, `Errors Encountered`.

A successful AutoCompact reduces Tier 3 by 60-80% in tokens while preserving the decision-level information.

### Tier 2 compaction (rare path)

If Tier 2 (Static Prefix) alone exceeds budget, the Compactor compresses Skill content within it:

```python
async def compact_tier_2(static_prefix: StaticPrefix, llm: LLMAdapter) -> StaticPrefix:
    # 1. Identify the largest LOADED_SKILLS entry
    # 2. Replace its full content with a "skill summary" produced by the LLM
    # 3. Mark in the resulting Static Prefix:
    #    [SKILL: name (compacted; full content available via load_skill('name'))]
```

The LLM can re-load the full skill via `load_skill` if it needs the detail. Tier 2 compaction invalidates the prompt cache from that point in the Conversation onward; in v0.2 this is acceptable (the path is rare).

### Memory extraction hook

Before any compaction (Micro or Auto) discards content, the Compactor invokes `MemoryWriter.extract_and_persist`:

```python
class MemoryWriter:
    async def extract_and_persist(
        self,
        *,
        session_id: SessionId,
        messages_to_drop: list[Message],
        compaction_mode: Literal["micro", "auto"],
    ) -> ExtractionResult:
        """Run extraction rules (and possibly an LLM call for Auto mode) to pull
        structured facts from the messages, then write them to Working Memory
        via the governance pipeline (ADR-0023)."""
```

For MicroCompact (no LLM call): rule-based extraction from tool messages — file paths read, search keywords used, tool call counts. Cheap, deterministic.

For AutoCompact (LLM-driven): the summarization output's `Decisions` / `Artifacts` / `Open Questions` sections feed directly into Memory write governance.

Extraction failures are logged but do not block compaction.

### Preservation rules (hard invariants)

Across both modes, the Compactor MUST preserve:

1. **Tier 1 (System Prompt)** — never touched.
2. **Tier 2 (Static Prefix)** — touched only in the rare Tier 2 path above.
3. **The first user message** in Tier 3 — the original task instruction.
4. **The last `preserve_recent_turns` turns** of Tier 3 — keeps the LLM oriented on the current state.
5. **Any message tagged `[COMPACTED_SUMMARY]`** — never re-compact a summary; it's the entry from a prior AutoCompact.
6. **Any tool call/result pair from the most recent turn** — the LLM may still be reasoning about it.

If preservation rules conflict with the size target (e.g., the preserved set alone exceeds the threshold), the Compactor logs an `OverPreservedError` and returns the unchanged messages. The Agent Core then surfaces the situation as `LLMContextOverflowError` and the orchestrator triggers stage retry.

### Interaction with Agent Core

The Agent Core invokes the Compactor at two points:

```python
# End of every turn, after appending tool results
if dynamic_tokens > cfg.micro_compact_soft_threshold or turn_index % cfg.micro_compact_every_n_turns == 0:
    messages = await compactor.micro_compact(messages)

# Beginning of every turn, before LLM call
if dynamic_tokens > cfg.auto_compact_hard_threshold:
    messages = await compactor.auto_compact(messages)

# On LLMContextOverflowError caught
except LLMContextOverflowError:
    messages = await compactor.auto_compact(messages, forced=True)
    # retry the LLM call once with compacted messages
```

### Observability

Compaction emits events on the EventBus (ADR-0015):

| Event | Payload |
|---|---|
| `compactor.micro.started` | session_id, conversation_id, tokens_before |
| `compactor.micro.completed` | tokens_after, messages_dropped, extraction_facts_count |
| `compactor.auto.started` | tokens_before, trigger (size / overflow / forced) |
| `compactor.auto.completed` | tokens_after, summary_length, extraction_facts_count |
| `compactor.failed` | reason (over_preserved / extraction_error / summarize_error) |

Dashboard renders these for a per-Conversation compaction timeline.

### Determinism

MicroCompact is deterministic given the same inputs — useful for testing and replay.

AutoCompact is non-deterministic (LLM-driven). To make it testable:

- The Mock LLM Adapter (ADR-0014) can be primed with a fixed summary for compaction calls.
- Replay (ADR-0009) records the compaction result alongside the Conversation; the replayed run uses the recorded summary instead of re-summarizing.

### Compaction in operation logs

Every compaction event is summarized in the operation log's `What was done` section by the Agent Core when it writes its own logs (ADR-0005). The body includes a one-line entry like:

```
- AutoCompact triggered at turn 12 (155k → 68k tokens; preserved 5 recent turns + 23 decisions).
```

This makes operation logs honest about how much context the stage's reasoning was based on.

### Failure handling

| Failure | Behavior |
|---|---|
| MicroCompact returns unchanged messages (no candidates to drop) | proceed; not an error |
| AutoCompact LLM call fails | retry once; if still failing → return original messages + emit `compactor.failed` |
| Memory extraction fails | log warning; compaction proceeds |
| Preservation rules conflict with size target | `OverPreservedError`; Agent surfaces as `LLMContextOverflowError` |
| AutoCompact summary itself overflows the budget | rare; treat as `OverPreservedError` |

## Consequences

- Long Conversations stay within context windows without the LLM hitting hard overflow errors mid-task.
- MicroCompact runs often and cheaply; AutoCompact runs rarely and expensively. This shape minimizes total LLM cost.
- Memory extraction happens BEFORE dropping content, so important facts survive even when verbose context is shed.
- Compaction is part of the operation log's narrative — reviewers see how much was preserved vs summarized, which builds trust in long-running stages.
- Replay determinism is preserved by recording AutoCompact summaries alongside the Conversation.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether MicroCompact heuristics need per-tool tuning (e.g., test results are always preserved; file reads always droppable) | Phase 3 implementation; gather data from real runs |
| Q2 | Token counting accuracy across providers — OpenAI vs Anthropic tokenizers differ | LLM Adapter (ADR-0014) responsibility to expose `count_tokens`; Compactor consumes it |
| Q3 | Whether AutoCompact summaries should be persisted as Episodic Memory entries (cross-session) | ADR-0020 |
| Q4 | Recursive compaction limit — if a Conversation needs AutoCompact twice in one turn (which shouldn't happen), what's the safeguard | Phase 3 implementation: hard cap at 2 AutoCompacts per turn |

## References

- ADR-0008 Session + Conversation Model
- ADR-0009 Agent Core
- ADR-0010 Context Layer Three-tier (Tier definitions)
- ADR-0014 LLM Adapter (token counting + compaction LLM call)
- ADR-0015 Observability (event names)
- ADR-0020 Memory Store four-layer
- ADR-0023 Memory Governance (write filter consumed by extraction hook)

## Reviewers

- [ ] Taven
