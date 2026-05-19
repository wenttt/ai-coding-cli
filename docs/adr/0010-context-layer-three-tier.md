# ADR-0010: Context Layer Three-tier Design

## Status

Proposed

## Date

2026-05-19

## Context

Specify the layered structure of the prompt sent to the LLM, the ordering rule that maximizes prompt-cache hit rate, the responsibilities of each tier, and the `ContextBuilder` interface.

## Decision

### Three tiers

```
┌─────────────────────────────────────────────────────┐
│  Tier 1: System Prompt        (highly cache-friendly)│  ← rarely changes
├─────────────────────────────────────────────────────┤
│  Tier 2: Static Prefix         (cache-friendly)      │  ← changes per session
├─────────────────────────────────────────────────────┤
│  Tier 3: Dynamic Context       (no cache)            │  ← changes every turn
└─────────────────────────────────────────────────────┘
```

The LLM message list is constructed in this exact order. Providers that implement prompt caching (Anthropic, OpenAI ≥ 2024) cache the longest common prefix. Putting stable content first maximizes hit rate.

### Tier 1: System Prompt

Content (set once per Agent invocation, never modified mid-loop):

- Agent role declaration ("You are the ai-coding-cli runtime…")
- Pipeline rules (Stage 1 is Issue-only, no git in design, etc.; same content that was previously in `.roorules` / `copilot-instructions.md` — now bundled in the package)
- Operation log writing contract (the agent's output MUST eventually call `write_operation_log` before terminating)
- Error policy (when to retry, when to ask the user)
- Tool calling discipline (do not invent tools, do not silently skip required tool calls)

Source: `src/ai_coding_cli/foundation/context/system_prompt.md` (bundled in the package, not template-rendered, identical across all invocations for a given version of the package).

This tier is the cache anchor — identical across all developers, all tickets, all stages, all turns within a package version.

### Tier 2: Static Prefix

Content (set when the Agent is constructed; immutable for the duration of one Conversation):

- **Project conventions** — read once from the workspace's `.ai-coding-cli/conventions.md` (if present): naming conventions, error-handling style, logging style, test framework, dependency rules.
- **Loaded Skills snapshot** — concatenated content of all Skills loaded into the Session at the time the Conversation starts (typically: pipeline-stage-specific skill + any user-added project skills). New skill loads during the conversation are NOT added here (they appear in Dynamic instead — see Skill loading mid-loop below).
- **Static repo context** — high-level repo facts: language list, framework names, top-level module list, public API surface (computed once on workspace open, cached).
- **Session-level facts** — the Jira ticket key, summary, type, the operation log file path that this Conversation will write to, the routing info (primary repo, workspace, role).

Source assembly: `StaticPrefixAssembler.assemble(session, loaded_skills, conventions, repo_facts) -> str`.

This tier is cache-friendly per Session: a developer working through Stage 1 → Stage 2 → Stage 3 on the same ticket reuses the same Static Prefix bytes if the loaded Skills don't change between conversations. (When a stage's skill changes, a new Conversation starts, the Static Prefix shifts, the cache hit drops on first turn only; subsequent turns of that Conversation hit the cache again.)

### Tier 3: Dynamic Context

Content (changes every turn or every stage):

- **Conversation message history** — assistant + tool + user messages accumulated during this Conversation.
- **The new user instruction** — the message the StageHandler passed to `Agent.run()`.
- **Retrieved context** — output of RAG / Graph queries the LLM has triggered (RAG retrievals appended as system messages with provenance tags).
- **Mid-loop skill loads** — Skills loaded by `load_skill` tool calls during the conversation appear here as system messages.
- **Prior operation log excerpts** — when a stage retries, the prior failed attempt's operation log (concise) is appended as Dynamic context for the new attempt.

This tier has zero cache value across invocations. Its size is the primary lever for total prompt cost.

### Message ordering on the wire

The OpenAI / Anthropic message format is a list. The Agent sends:

```python
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},                # Tier 1
    {"role": "system", "content": STATIC_PREFIX_BLOCK},          # Tier 2 (single big system msg)
    *DYNAMIC_MESSAGES,                                            # Tier 3 (assistant/user/tool interleaved)
]
```

Tier 1 and Tier 2 are each a single system message (concatenated content, not split). This is essential for prompt caching: many providers cache contiguous prefix blocks; multiple small system messages can fragment the cache key.

The cached-prefix marker (when the provider supports an explicit marker, e.g. Anthropic's `cache_control` field) is placed at the end of Tier 2.

### ContextBuilder interface

```python
class ContextBuilder:
    """Assembles the message list for an Agent turn."""

    async def build_initial(
        self,
        *,
        session: Session,
        conversation: Conversation,
        new_user_message: str,
    ) -> list[Message]:
        """First-turn assembly: System + Static + initial Dynamic (just the user message)."""

    async def append_user_message(
        self, messages: list[Message], user_message: str
    ) -> list[Message]: ...

    async def append_assistant_message(
        self, messages: list[Message], response: LLMResponse
    ) -> list[Message]: ...

    async def append_tool_results(
        self, messages: list[Message], results: list[ToolResult]
    ) -> list[Message]: ...

    async def inject_retrieved_context(
        self, messages: list[Message], retrieved: list[RetrievedSnippet]
    ) -> list[Message]:
        """Insert RAG / Graph retrieval results as system messages, tagged with provenance."""

    async def inject_loaded_skill(
        self, messages: list[Message], skill: LoadedSkill
    ) -> list[Message]:
        """Insert mid-loop skill content as a system message with [SKILL:name] tag."""
```

`build_initial` is called once at Agent.run() entry. The other methods mutate the message list in place (returning the same list for chainability), invoked by the Agent Core throughout the loop.

### Static Prefix shape

A single system message in the form:

```
[PROJECT CONVENTIONS]
{conventions.md content, if present}

[REPO FACTS]
- Languages: Python, TypeScript
- Frameworks: FastAPI, React
- Top-level modules: src/auth/, src/api/, src/web/, src/common/
- ...

[SESSION]
- Jira ticket: PROJ-123 — Add OAuth login
- Type: user_story
- Mode: brownfield
- Workspace: /Users/dev/proj
- Operation log: docs/operations/PROJ-123/{NN}-{stage}-v{N}.md

[LOADED SKILLS]
[SKILL: design-brownfield]
{skill content}
[/SKILL]

[SKILL: another-skill]
{skill content}
[/SKILL]
```

Bracketed section headers are used so the LLM can refer to them ("per [PROJECT CONVENTIONS]…"). The order is fixed; new sections at the end if needed in future versions.

### Dynamic Context shape

Standard OpenAI message list semantics. Retrieved context and mid-loop skill loads are injected as system messages with a tag prefix:

```
{"role": "system", "content": "[RAG: similar past tickets]\n\n--- PROJ-67 ---\n…\n--- PROJ-92 ---\n…"}
{"role": "system", "content": "[SKILL: mid-loop loaded]\n…"}
```

These system-tagged messages sit between user / assistant / tool messages in their insertion order. The LLM is instructed in Tier 1 to treat them as authoritative context, not as the user speaking.

### Token budget allocation

Default budget (per Agent invocation, fits within `max_total_tokens = 200_000`):

| Tier | Budget (tokens) | Notes |
|---|---|---|
| Tier 1 (System Prompt) | ~3,000 | Stable |
| Tier 2 (Static Prefix) | ~15,000 | Conventions + repo facts + loaded skills |
| Tier 3 (Dynamic Context) | ~150,000 | Grows with conversation; Compactor (ADR-0011) keeps it under cap |
| Headroom for completion | ~30,000 | Reserved for the model's response |

Static Prefix overrun is rare; if it happens (e.g., a very large skill loaded), the Compactor compresses inside the Static Prefix block (see ADR-0011) — a less common path than Dynamic compaction.

### Cache-control hints

When the LLM Adapter (ADR-0014) detects Anthropic models, it adds `cache_control: {type: "ephemeral"}` to the last Static Prefix block. When it detects OpenAI prompt caching (GPT-4o+), no explicit marker is needed (caching is automatic on identical prefixes).

For models without explicit caching support, the layered structure still helps if the provider has implicit caching; for models with no caching at all, the structure is harmless (no overhead).

### Conventions file (`.ai-coding-cli/conventions.md`)

Optional file in the workspace. When present, included in Static Prefix. Recommended sections (not enforced):

```markdown
# Project conventions

## Error handling

- All exceptions extend `AppError` base class.
- Error codes follow `ERR-{MODULE}-{NUMBER}` format.

## Logging

- Structured logging via `structlog`; JSON output in production.
- Never log raw user PII.

## Tests

- pytest with pytest-asyncio.
- Test files mirror src/ structure under tests/unit/.

## Dependencies

- New dependencies require ADR + team review.
- Avoid pinning to alpha/beta releases.

## Naming

- Modules: snake_case
- Classes: PascalCase
- Constants: SCREAMING_SNAKE_CASE
```

When absent, Static Prefix omits the conventions section.

### Skill loading mid-loop

When `load_skill(name)` is called by the LLM during the Conversation (ADR-0009, ADR-0012), the Skill Loader returns content. The `ContextBuilder.inject_loaded_skill` method appends a system message in Dynamic Context (NOT Static Prefix — modifying Static mid-loop invalidates the cache for the rest of the conversation, which is undesirable):

```
{"role": "system", "content": "[SKILL: load-skill-name (loaded mid-conversation at turn N)]\n…content…"}
```

The LLM sees the skill content from that turn onward.

### Conversation continuation

When a Conversation resumes from a paused state (rare in v0.2 — typically a single run from start to terminal output), the ContextBuilder reconstructs the full Tier 3 from `Conversation.messages_json`. Tier 1 and Tier 2 are recomputed (Tier 2 may have changed if Skills were added between the pause and resume).

### Failure handling

| Failure | Behavior |
|---|---|
| Tier 2 total size exceeds budget | Trigger Compactor on Static Prefix (rare); if still oversize after compaction → FatalError |
| Tier 3 message append makes total exceed budget | Trigger Compactor on Tier 3; if still oversize → fail this turn with `LLMContextOverflowError` |
| conventions.md exists but unparseable | Log warning, skip; do not block Agent |
| Loaded skill content is empty | Log warning, omit from Static Prefix |
| Provider doesn't recognize cache_control marker | Adapter silently strips the field; no error |

## Consequences

- The three-tier structure is the foundation for prompt caching across providers. Cache hit rate on Tier 1 + Tier 2 is the primary cost lever; we expect 60-80% cache hit on the long stable prefix once a Conversation is mid-flight.
- Static Prefix immutability per Conversation simplifies caching reasoning: any change forces a new Conversation, which means a new cache key from turn 1.
- The convention of placing retrieved context and mid-loop skills as system messages in Dynamic (not in Static) is deliberate: it preserves cache continuity at the cost of slightly less coherent message threading.
- Bracketed section headers (`[PROJECT CONVENTIONS]`, `[RAG: …]`, `[SKILL: …]`) give the LLM stable anchors for referring back to context segments.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | How conventions.md content is validated / linted to avoid LLM-jailbreaking content | ADR-0025 (input guardrail covers user-controlled file inputs) |
| Q2 | Whether repo facts should be regenerated per-conversation or per-session | Implementation: per-session, refreshed when workspace HEAD changes |
| Q3 | Token budget profiling — measuring actual cache hit rates per provider | Phase 2 deliverable |
| Q4 | Should Static Prefix be split into multiple system messages when very large, sacrificing some cache granularity for working-set fit | Phase 2 perf decision |

## References

- ADR-0001 System Overview
- ADR-0008 Session + Conversation Model
- ADR-0009 Agent Core (consumer of ContextBuilder)
- ADR-0011 Compactor (planned)
- ADR-0012 Skill Loader (planned)
- ADR-0014 LLM Adapter (cache_control hints)
- ADR-0025 Guardrail Layer (input guardrail for user-controlled context)

## Reviewers

- [ ] Taven
