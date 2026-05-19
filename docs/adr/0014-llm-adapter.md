# ADR-0014: LLM Adapter

## Status

Accepted

## Date

2026-05-19

## Context

Specify the LLM provider abstraction: interface, shipped implementations (OpenAI-compatible, Anthropic native, Mock), token counting, cache-control hints, summarization-for-compaction, error mapping, configuration.

## Decision

### Provider abstraction

```python
class LLMAdapter(Protocol):
    """Provider-agnostic chat completion."""

    provider_name: str                           # "openai-compat" | "anthropic" | "mock"
    model_name: str                              # the specific model in use
    supports_tool_calling: bool
    supports_streaming: bool
    supports_prompt_cache_hints: bool

    async def complete(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 300.0,
        cache_hints: CacheHints | None = None,
    ) -> LLMResponse: ...

    async def summarize_for_compaction(
        self,
        *,
        messages: list[Message],
        instructions: str,
        max_tokens: int = 4000,
    ) -> str:
        """Special-purpose call used by the Compactor. MAY route to a cheaper
        model than the primary `model_name`."""

    def count_tokens(self, content: str | list[Message] | list[ToolSchema]) -> int:
        """Provider-aware token estimate. Used by Context Layer + Compactor."""


@dataclass(frozen=True)
class LLMResponse:
    content: str | None                          # the assistant message text
    tool_calls: list[ToolCall]                   # parsed from provider's response
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter", "other"]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int = 0                    # tokens served from prompt cache, if reported
    raw_provider_response: dict | None = None    # for debug / replay


@dataclass(frozen=True)
class CacheHints:
    """Adapter-specific. The Adapter translates these to provider format."""
    cacheable_prefix_end_index: int | None       # the index of the last system message that should be cached
    ephemeral: bool = True                        # short-TTL caching (typical)
```

### Shipped implementations

#### 1. OpenAICompatibleAdapter

Covers any endpoint that speaks the OpenAI Chat Completions API:

- OpenAI (`api.openai.com`)
- Azure OpenAI
- Internal company gateways
- vLLM, TGI, Together, Groq, Fireworks
- Anthropic via their OpenAI-compat shim

Constructor:

```python
OpenAICompatibleAdapter(
    base_url="https://llm.company.com/v1",
    api_key=...,
    model_name="gpt-4o",
    headers={"X-Tenant-Id": "..."},               # optional extra headers
    organization_id=None,
    request_logging=True,                          # log full request bodies at DEBUG (off by default in prod)
)
```

Behavior:

- Uses the official `openai` Python SDK pointed at `base_url`.
- Tool calling: passes `tools=...` and `tool_choice="auto"` per OpenAI spec; parses `response.choices[0].message.tool_calls`.
- Streaming: implemented but not consumed by the Agent in v0.2 (per ADR-0009).
- Prompt cache: GPT-4o+ caches automatically on identical prefixes; the adapter emits no explicit marker. `cache_hit_tokens` is populated from `usage.prompt_tokens_details.cached_tokens` when the API reports it.

#### 2. AnthropicNativeAdapter

For Anthropic's Claude API directly (not via OpenAI shim). Reason: native Anthropic protocol exposes explicit `cache_control` markers and Anthropic-specific features (extended thinking, citations) that the OpenAI shim flattens away.

```python
AnthropicNativeAdapter(
    api_key=...,
    model_name="claude-3-5-sonnet-20241022",
    anthropic_version="2023-06-01",
    cache_control_default="ephemeral",
)
```

Behavior:

- Uses the official `anthropic` Python SDK.
- Converts OpenAI-format messages to Anthropic's structure (separate `system` parameter, `user`/`assistant` interleaved, tool_use / tool_result blocks).
- Tool calling: translates OpenAI tool schemas to Anthropic's tool definition format; parses `tool_use` blocks from the response.
- Cache hints: when `CacheHints.cacheable_prefix_end_index` is set, injects `cache_control: {type: "ephemeral"}` on the corresponding message block.
- `cache_hit_tokens` from `usage.cache_read_input_tokens`.

#### 3. MockAdapter

For testing. Pre-programmed with responses:

```python
mock = MockAdapter()
mock.add_response(
    on_messages_containing="read the design",
    response=LLMResponse(
        content=None,
        tool_calls=[ToolCall("read_repo_file", {"path": "docs/designs/PROJ-1.md"})],
        finish_reason="tool_calls",
        prompt_tokens=1500, completion_tokens=50, total_tokens=1550,
    ),
)
mock.add_compaction_summary(when_messages_count_gte=20, summary="...")
```

Used by Agent-level integration tests + Replay (ADR-0009).

`MockAdapter.count_tokens` uses a deterministic estimator (4 chars per token); `summarize_for_compaction` returns a programmed summary.

### Token counting

Each adapter implements `count_tokens` using the provider's official tokenizer when available:

- OpenAICompatibleAdapter (OpenAI models): `tiktoken` with the model's encoding.
- OpenAICompatibleAdapter (non-OpenAI providers): fall back to `tiktoken` cl100k_base or, if `provider` declares its own tokenizer in config, use that.
- AnthropicNativeAdapter: `anthropic.tokenizer` (Anthropic-specific).
- MockAdapter: `len(content) // 4` (deterministic).

Counting accuracy matters for the Compactor (ADR-0011) and the Context Layer budget (ADR-0010). Over-count is safer than under-count — adapters round up when uncertain.

For tools, `count_tokens(tools)` counts the serialized JSON schema. Tool schemas are part of the prompt cost.

### Cache-control hints

The Context Layer (ADR-0010) builds messages with Tier 1 + Tier 2 as stable prefix. When passing to the adapter, it sets:

```python
cache_hints = CacheHints(
    cacheable_prefix_end_index=len(tier_1_msgs) + len(tier_2_msgs) - 1,
    ephemeral=True,
)
```

Adapter behavior:

| Adapter | Behavior with cache_hints |
|---|---|
| OpenAICompatibleAdapter against gpt-4o+ | No-op; OpenAI caches automatically. Reports `cache_hit_tokens` from response. |
| OpenAICompatibleAdapter against gpt-3.5 / others | No-op; provider doesn't cache. |
| AnthropicNativeAdapter | Injects `cache_control: {type: "ephemeral"}` on the message at `cacheable_prefix_end_index`. |
| MockAdapter | No-op. |

If `cache_hits.cacheable_prefix_end_index >= len(messages)`, the adapter logs a warning and ignores the hint (defensive — should not happen).

### Summarization for compaction

The Compactor (ADR-0011) calls `summarize_for_compaction` to compress middle conversation segments. The implementation:

```python
async def summarize_for_compaction(self, messages, instructions, max_tokens=4000):
    summary_messages = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": self._render_messages_for_summary(messages)},
    ]
    response = await self.complete(
        messages=summary_messages,
        tools=None,
        max_tokens=max_tokens,
        temperature=0.0,
        cache_hints=None,                # summarization is one-shot; caching does not help
    )
    return response.content or ""
```

A configurable `compaction_model` overrides `model_name` for this call (typically a smaller / cheaper model). Example: `compaction_model="gpt-4o-mini"` while `model_name="gpt-4o"`.

### Streaming

The `complete()` interface awaits the full response. A separate `stream()` method is reserved (returns an async iterator of `StreamingChunk`) but the Agent Core does not call it in v0.2.

Adapters implement `stream()` as a stub if their provider supports it; the underlying SDKs (`openai`, `anthropic`) already handle the protocol.

### Configuration

```python
class LLMConfig(BaseModel):
    primary_adapter: AdapterConfig
    fallback_adapter: AdapterConfig | None = None    # used when primary returns LLMRateLimitError repeatedly
    compaction_adapter: AdapterConfig | None = None  # used for summarize_for_compaction; falls back to primary if None
    request_timeout_seconds: float = 300.0
    rate_limit_retry_max: int = 3
    rate_limit_retry_base_seconds: float = 2.0

class AdapterConfig(BaseModel):
    kind: Literal["openai-compat", "anthropic-native", "mock"]
    model_name: str
    base_url: str | None = None
    api_key: SecretStr | None = None
    extra: dict[str, Any] = {}                       # adapter-specific options
```

Loaded from `.env` per ADR-0016. The daemon constructs adapters from `LLMConfig` at startup.

### Multi-adapter fallback

If `primary_adapter` exhausts `rate_limit_retry_max` attempts, the daemon switches to `fallback_adapter` for the remainder of the current Agent invocation (not permanently — next invocation tries primary again). This is opt-in; v0.2 default is no fallback.

The Compactor uses `compaction_adapter` when set, otherwise the primary. Operation logs record which adapter served which call (for cost analysis).

### Error mapping

Each adapter translates provider-specific errors into the AgentError taxonomy (ADR-0009):

| Provider error | Mapped to |
|---|---|
| HTTP 429 (rate limit) | `LLMRateLimitError` |
| HTTP 5xx | `LLMRateLimitError` (transient; retried) |
| HTTP 408 / asyncio timeout | `LLMTimeoutError` |
| HTTP 400 (bad request) — invalid tool schema | `LLMInvalidResponseError` (FatalError) |
| HTTP 401 / 403 | `FatalError` (configuration / auth issue) |
| Response body unparseable JSON | `LLMInvalidResponseError` |
| Context length exceeded (provider-specific signal) | `LLMContextOverflowError` |
| Content filter triggered | `GuardrailViolation` |

The Agent Core's retry policy (ADR-0009) handles `RetryableError` (rate limit, timeout). FatalErrors surface to the orchestrator.

### Cache_hit_tokens reporting

When the provider reports cache hit data (Anthropic always; OpenAI on gpt-4o+ in recent API versions), the adapter populates `LLMResponse.cache_hit_tokens`. The Observability event `turn.ended` carries this field; the Dashboard renders a per-Conversation cache-hit ratio.

For providers that don't report cache hits, `cache_hit_tokens = 0`.

### Replay support

`Replay` mode (ADR-0009 + ADR-0011) requires deterministic LLM responses. The MockAdapter is the standard replay tool: load a recorded conversation, prime the mock with each recorded `LLMResponse` in order, run the Agent, assert that tool calls + final outcome match.

The `raw_provider_response` field on `LLMResponse` is preserved for replay fidelity — when a recorded response is replayed, the mock returns the exact same provider-format payload (lets the Agent's response-parsing code run unchanged).

### Token usage telemetry

`turn.ended` event (ADR-0009) carries `prompt_tokens` / `completion_tokens` / `total_tokens` / `cache_hit_tokens` for every LLM call. The Observability subsystem aggregates these:

- Per-Session token totals
- Per-stage token totals
- Per-adapter token totals
- Cache hit ratio

Surface in Dashboard + as Prometheus metrics (ADR-0015).

### Sample provider-config matrix

```yaml
# .env
LLM_PRIMARY_KIND=openai-compat
LLM_PRIMARY_BASE_URL=https://llm.company-internal.com/v1
LLM_PRIMARY_API_KEY=...
LLM_PRIMARY_MODEL=gpt-4o

LLM_COMPACTION_KIND=openai-compat
LLM_COMPACTION_BASE_URL=https://llm.company-internal.com/v1
LLM_COMPACTION_API_KEY=...
LLM_COMPACTION_MODEL=gpt-4o-mini

LLM_FALLBACK_KIND=anthropic-native
LLM_FALLBACK_API_KEY=...
LLM_FALLBACK_MODEL=claude-3-5-sonnet-20241022
```

Or for a developer who only has Anthropic API access:

```yaml
LLM_PRIMARY_KIND=anthropic-native
LLM_PRIMARY_API_KEY=sk-ant-...
LLM_PRIMARY_MODEL=claude-3-5-sonnet-20241022
```

### Failure handling

| Failure | Behavior |
|---|---|
| Primary adapter unavailable at startup (auth fails) | daemon refuses to start; clear error in stderr |
| Provider returns malformed `tool_calls` JSON | Adapter raises `LLMInvalidResponseError` (FatalError); operation log records the bad payload for debugging |
| Cache hint provided but provider doesn't support cache_control | Adapter silently drops the field; logs at DEBUG |
| Streaming requested but provider doesn't support | NotImplementedError at the adapter level; Agent doesn't call stream() in v0.2 |
| `count_tokens` differs from actual prompt_tokens by > 10% | Logged at WARN; not an error (provider tokenizer evolution can cause this) |

## Consequences

- Adapters are swappable; switching from company-internal to OpenAI to Anthropic is a config change, no code change.
- Cache-control behavior is uniform from the Context Layer's perspective; the adapter handles provider-specific quirks.
- Compaction can use a cheaper model than the primary, materially reducing total cost on long conversations.
- Replay works because Mock + raw_provider_response preserve the exact LLM payloads.
- Token counting accuracy is provider-aware, giving the Compactor and Context Layer reliable budgets.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Streaming integration in post-v0.2 — partial tool-call assembly + partial Guardrail invocation | Post-v0.2 design |
| Q2 | Whether to ship a unified pricing table per provider for cost telemetry (current: tokens only, cost computed downstream) | Phase 8 (production readiness) |
| Q3 | How to handle providers that return tool calls in non-standard formats (some open-source models) — adapter-level parser tolerance | Phase 2 implementation tuning |
| Q4 | Anthropic extended thinking mode (when content includes thinking blocks) — surface to Context Layer or strip? | Phase 2; default strip until use case appears |

## References

- ADR-0001 System Overview
- ADR-0009 Agent Core (consumes LLMAdapter.complete; AgentError mapping)
- ADR-0010 Context Layer (cache_hints construction)
- ADR-0011 Compactor (uses summarize_for_compaction)
- ADR-0015 Observability (token usage events)
- ADR-0016 Configuration management (LLMConfig)

## Reviewers

- [ ] Taven
