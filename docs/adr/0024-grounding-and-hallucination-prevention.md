# ADR-0024: Grounding + Hallucination Prevention

## Status

Proposed

## Date

2026-05-19

## Context

Specify the techniques used to keep the agent's output anchored to verifiable facts: provenance tagging, read-after-write verification, schema constraints on tool outputs, agent_claim vs tool_grounded separation in Memory, and the guardrail hooks that enforce these.

## Decision

### Two-class claim model

Every fact the agent acts on is one of two kinds:

```
GROUNDED FACT     — sourced from a tool call result; verifiable against the source
AGENT CLAIM       — the LLM said it; no external source; treat with skepticism
```

The distinction is preserved everywhere downstream:

- `MemoryEntry.source.kind` — `tool_grounded` vs `agent_output` (ADR-0020)
- `OperationLog.outputs.artifacts` — only populated from tool results, never from LLM prose
- Context Layer Tier 3 — retrieved snippets carry `ProvenanceTag` so the LLM can see which facts are grounded vs derived

### Provenance tagging

Every persisted fact carries a `Provenance` object:

```python
@dataclass(frozen=True)
class Provenance:
    kind: Literal["tool_grounded", "agent_output", "human_curated"]
    tool_call_invocation_id: str | None        # for tool_grounded
    operation_log_id: int | None               # the log this fact was first recorded in
    conversation_id: UUID | None
    turn_index: int | None                     # 0-based turn within the conversation
    extracted_at: datetime
    extraction_method: Literal["direct_value", "json_path", "regex", "llm_extracted", "human_input"]
    extraction_detail: str | None              # e.g., JSONPath expression or regex pattern
```

`extraction_method` distinguishes:

| method | what it means |
|---|---|
| `direct_value` | the value is literally the tool's return — strongest |
| `json_path` | a JSONPath selector pulled the value from a tool's JSON result |
| `regex` | a regex extracted from text — weaker; pattern stored in `extraction_detail` |
| `llm_extracted` | an LLM call (e.g., compaction summarizer) extracted this — weakest grounding |
| `human_input` | a human directly provided this via CLI / Dashboard |

Confidence (ADR-0023) is biased by `extraction_method` independent of `source.kind`:

| method | confidence ceiling |
|---|---|
| `direct_value` | 1.00 |
| `json_path` | 0.95 |
| `regex` | 0.80 |
| `llm_extracted` | 0.65 |
| `human_input` | 1.00 |

If a writer claims `source.kind = "tool_grounded"` but provides `extraction_method = "llm_extracted"`, governance (ADR-0023) caps the confidence at 0.65 regardless of source baseline.

### Read-after-write verification

When the agent claims to have done something with an external side effect, the next turn MUST verify via a tool call. The Agent Core enforces this via a system prompt rule:

```
After any action you take that produces a side effect (writing a file,
creating a Jira ticket, opening a PR, transitioning a status), your next
tool call MUST verify the result. Use `read_repo_file`,
`read_jira_ticket`, `get_pr_state`, `get_issue_state`, etc.

If you produce a final assistant message claiming a side effect without
having verified it via a tool call in the same conversation, the
operation log records this as `unverified_claim` and confidence on
related Memory writes is downgraded.
```

The Agent Core inspects the final message + the preceding tool calls. The detection:

```python
class ReadAfterWriteAnalyzer:
    def analyze(self, conversation: Conversation) -> list[UnverifiedClaim]:
        """For each tool call in the conversation whose tool's side_effects class is
        LOCAL_WRITE / EXTERNAL_WRITE / DESTRUCTIVE, check whether a subsequent
        tool call read back the affected resource. If not, flag."""

@dataclass(frozen=True)
class UnverifiedClaim:
    tool_name: str
    invocation_id: str
    turn_index: int
    side_effects: list[SideEffectRecord]
    suggested_verification: str       # e.g., "read_repo_file with path=..."
```

Detection rules per side-effect class:

| Side effect | Required verification |
|---|---|
| `LOCAL_WRITE` (write_repo_file) | a subsequent `read_repo_file` of the same path within the same conversation |
| `LOCAL_WRITE` (git_commit) | a subsequent `git_log` or `git_status` |
| `EXTERNAL_WRITE` (create_design_issue) | a subsequent `read_github_issue` of the returned issue_number |
| `EXTERNAL_WRITE` (create_pr) | a subsequent `get_pr_state` |
| `EXTERNAL_WRITE` (transition_jira_status) | a subsequent `read_jira_ticket` |
| `EXTERNAL_WRITE` (add_jira_comment) | not required (comments are not high-stakes; the tool returns a comment_id which is enough) |
| `DESTRUCTIVE` (git_push_force, trigger_deployment) | a subsequent `get_pr_state` / `verify_deployment` |

Unverified claims are recorded as warnings in the operation log's "What I could not do" section. They do not halt the agent, but they downgrade related Memory writes' confidence.

### Schema constraints on tool outputs

Tool outputs are Pydantic-validated (`Tool.output_model`, ADR-0013). An invalid output is treated as a tool error and surfaces to the LLM:

- LLM hallucinated a tool call with malformed arguments → caught at input validation (Tool Registry, ADR-0013) → returned as ToolResult.error → LLM sees and corrects.
- A real tool returned malformed data (provider bug) → ToolResult.error → operation log records the bad payload for debugging.

The LLM cannot bypass tool schema. This is the strongest grounding control we have.

### Tool result citation discipline

When the LLM cites a tool result in its assistant message, it must reference the tool by name + invocation. The system prompt (Tier 1) instructs:

```
When you cite a fact from a tool result, format it as:

  > Per `tool_name` (invocation N): <fact summary>

For tool calls the user can reference back to the recording.
```

The convention is advisory in v0.2 — we don't reject messages that omit citations. But operation log writers extract citations from assistant text where they appear, and Memory writes that include citations carry stronger `Provenance` (extraction_method = `direct_value` when the citation is verbatim).

### Memory separation: claim layer

Memory entries have a `source.kind` field. We add a derived view that filters by source class:

```python
class MemoryReader:
    async def search_similar(
        self,
        *,
        query_text: str,
        ...
        grounded_only: bool = False,            # only return tool_grounded + human_curated
    ) -> list[ScoredMemoryEntry]: ...
```

The `Context Layer` (ADR-0010) and the `mcp-self-review` skill default to `grounded_only=True` when retrieving facts used to make assertions. Discovery flows (e.g., `mcp-investigate` brainstorming root cause) use `grounded_only=False` for broader recall.

### Operation log honesty section

The "What I could not do" section of every operation log (ADR-0005) is the explicit honesty surface. The agent writes here:

- Tools attempted but failed
- Required information that wasn't available
- Decisions made under uncertainty (with reasoning)
- Unverified claims (auto-populated by `ReadAfterWriteAnalyzer`)
- Memory writes that were rejected by governance

A stage handler that produces no entries in "What I could not do" while the conversation contained errors, retries, or guardrail refusals triggers a validation warning at log write time.

### Anti-fabrication system-prompt rules

Tier 1 System Prompt (ADR-0010) includes these rules:

```
1. Do not invent tool names. Only call tools listed in the AVAILABLE TOOLS
   section. If you need a tool that does not exist, say so in your message;
   do not synthesize one.

2. Do not fabricate file paths, function names, or schema fields. Read the
   source first via read_repo_file, list_repo_files, find_relevant_modules.

3. Do not invent ticket keys, PR numbers, or commit SHAs. These come from
   tool results. If you reference one, it must have appeared in a tool
   result earlier in the conversation.

4. Do not invent Jira transitions or GitHub label names. Call
   read_jira_ticket / get_pr_state to see valid values.

5. When you claim to have written a file, opened an Issue, or made a
   commit, your NEXT tool call must verify it. Failing to do so means the
   operation log records `unverified_claim` and downstream Memory writes
   lose confidence.

6. When uncertain, say so. Saying "I'm not sure whether X" is preferred
   over asserting X without grounding.
```

These rules don't prevent hallucination at the LLM level (no rule does); they make hallucinations recoverable + auditable by tooling.

### Schema validation for design Issues

Cross-project designs (ADR-0004) must include a parseable Contract section (OpenAPI / Protobuf / GraphQL). The handler runs syntactic validation before writing the Issue:

```python
def validate_contract_section(contract_type: str, contract_text: str) -> ValidationResult:
    if contract_type == "openapi":
        try:
            import openapi_spec_validator
            openapi_spec_validator.validate(yaml.safe_load(contract_text))
        except ...:
            return ValidationResult.failed(...)
    elif contract_type == "protobuf":
        # protoc syntax check via subprocess
        ...
    elif contract_type == "graphql":
        # graphql-core parser
        ...
    return ValidationResult.ok()
```

A failed contract validation is a `RetryableError` — the agent re-runs with the validation error in its next prompt.

### Provenance tagging in retrieved context

When `inject_retrieved_context` (ADR-0010) appends snippets to Tier 3:

```
[RAG: 3 results]

1. [grounded; operation_log #4521 (PROJ-67 implement-v2, 2026-04-12)]
   Made a similar OAuth refresh fix; rolled back due to flaky test;
   later resolved by stubbing the provider.

2. [agent_claim; conversation a3f9 turn 7]
   The team uses error prefix `AUTH-` for auth-module exceptions.

3. [grounded; rag_chunk source code_file src/auth/legacy/refresh.py:34-67]
   Legacy refresh logic; preserved for backward compatibility (see PROJ-12).
```

The bracketed tag tells the LLM whether to trust the snippet for assertions or treat it as a hint. This bias shifts behavior: when the LLM cites #2 in its output, it can be conservatively phrased ("The team appears to use prefix AUTH-…") rather than asserted as fact.

### Tool result trust levels

Some tools' results are more trustable than others:

| Tool | Trust |
|---|---|
| `read_repo_file` | full — reads exact file content |
| `git_diff` | full — reads exact git output |
| `list_repo_files` | full — fs listing |
| `read_jira_ticket` | full — Jira API |
| `read_github_issue` | full — GitHub API |
| `find_relevant_modules` | high — keyword search results are deterministic |
| `analyze_repo_state` | high — heuristics but deterministic |
| `run_tests` | full — actual test output |
| `git_log` | full — git output |
| `lookup_project_for_ticket` | full — config-derived |
| `affected_projects_for_ticket` | high — derived from labels + components per rules in ADR-0006 |
| `discover_test_framework` | high — heuristic |

The default is "full trust" for native tools. MCP-bridged tools (ADR-0013) default to "medium trust" since their implementations are external. The agent's prompt does not explicitly call this out; the trust is implicit in how the operation log records the citation.

### Hallucination metrics

We track (for telemetry, post-v0.2):

- `hallucination_metric_unverified_claims_per_conversation` — count from `ReadAfterWriteAnalyzer`
- `hallucination_metric_fabricated_tool_calls_per_conversation` — count of `ToolNotFoundError` outcomes
- `hallucination_metric_invalid_args_per_conversation` — count of `ToolArgumentValidationError` outcomes

These appear on the Dashboard per Conversation + as aggregate metrics. A high rate flags the stage / skill / agent for review.

### CLI

```
ai-coding grounding analyze <conversation_id>
    # show all unverified claims + their suggested verifications

ai-coding grounding stats --since YYYY-MM-DD
    # aggregate hallucination metrics over a window
```

### Failure handling

| Failure | Behavior |
|---|---|
| `ReadAfterWriteAnalyzer` flags claims | Recorded in operation log "What I could not do"; agent does NOT halt; Memory writes downgrade confidence |
| Contract syntactic validation fails | `RetryableError`; agent retries within stage retry budget |
| Tool returns malformed payload | ToolResult.error; LLM sees + corrects in next turn |
| LLM cites a tool result that doesn't exist in the conversation | Caught by `inject_retrieved_context` — fictitious citations don't render with provenance tags; LLM's output may still pass without correction (we don't pre-screen output; that's Guardrail's job, ADR-0025) |
| Provenance object missing required fields | Memory governance Stage 1 rejects the write |

## Consequences

- Grounding is enforced through three layers: schema validation (Tool Registry rejects malformed args + outputs), read-after-write detection (analyzer flags unverified claims), and provenance tagging (downstream consumers know how strongly to trust each fact).
- Memory writes that lack grounding fall to low confidence automatically, so retrieval surfaces stronger facts preferentially.
- The "What I could not do" section becomes the canonical honesty surface: humans can audit at a glance whether a stage proceeded with unverified claims.
- Anti-fabrication rules in the system prompt don't prevent hallucination at the model level — they make hallucinations surface as auditable events rather than silent errors.
- Contract validation in cross-project design ensures a fabricated OpenAPI / Protobuf / GraphQL fails fast, before sub-tickets fan out.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Whether to actively block assistant messages that fail `ReadAfterWriteAnalyzer` (currently advisory) | Phase 6 (Guardrail) — possibly via Output Guardrail |
| Q2 | Provenance for facts extracted by the compaction LLM (currently `llm_extracted`; could be more granular) | Phase 4 implementation |
| Q3 | LLM-based fact-checking against the workspace (deeper than Read-after-write) | Post-v0.2 |
| Q4 | Handling tools that intrinsically don't have verifiable outputs (e.g., a "summarize this" tool — the summary itself is the value) | Phase 4 |

## References

- ADR-0005 Operation Log Schema ("What I could not do" section)
- ADR-0009 Agent Core (operation log validation)
- ADR-0010 Context Layer (Tier 3 retrieved-context tagging)
- ADR-0013 Tool Registry (output_model schema validation)
- ADR-0020 Memory Store four-layer (source.kind)
- ADR-0023 Memory Governance (confidence modifiers by extraction_method)
- ADR-0025 Guardrail Layer (Output Guardrail may use these analyzers)

## Reviewers

- [ ] Taven
