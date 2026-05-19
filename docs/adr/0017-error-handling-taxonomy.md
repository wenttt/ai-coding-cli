# ADR-0017: Error Handling Taxonomy

## Status

Accepted

## Date

2026-05-19

## Context

Consolidate the error taxonomy referenced by prior ADRs (0009, 0011, 0013, 0014, 0016) into one canonical reference. Specify base classes, subsystem leaves, propagation rules, and how errors translate to Tool results, Stage results, operation logs, Jira comments, and user-facing CLI messages.

## Decision

### Base hierarchy

```
AgentError                              # foundation/errors.py
├── RetryableError                      # transient; retry the same call
├── FatalError                          # unrecoverable; halt the current invocation
└── UserAbort                           # user-initiated termination; no retry
```

Three categories cover every error in the system. Subsystems define leaves under the appropriate base.

```python
class AgentError(Exception):
    """Base for all errors in the agent runtime."""
    code: str                       # stable identifier, e.g. "LLM_RATE_LIMIT"
    user_message: str | None        # human-friendly explanation; None defaults to repr(self)
    cause: Exception | None         # original exception chain
    context: dict[str, Any]         # structured diagnostic info

    def __init__(self, message: str, *, code: str | None = None,
                 user_message: str | None = None, cause: Exception | None = None,
                 **context): ...


class RetryableError(AgentError):
    """Transient. The same call MAY succeed if retried, optionally after a delay."""
    retry_after_seconds: float | None = None    # provider-suggested delay, if known


class FatalError(AgentError):
    """Unrecoverable for the current invocation. Halts the loop / stage / handler."""


class UserAbort(AgentError):
    """User-initiated termination (SIGINT, Ctrl+C, daemon stop signal)."""
```

### Subsystem leaves

Errors below are concrete subclasses of one of the three bases. They are organized by subsystem; each is `class X(<Base>)` with a static `code = "..."` for stable identification.

#### LLM Adapter (ADR-0014)

| Class | Base | Code | When |
|---|---|---|---|
| `LLMRateLimitError` | Retryable | `LLM_RATE_LIMIT` | HTTP 429 |
| `LLMTimeoutError` | Retryable | `LLM_TIMEOUT` | request timeout |
| `LLMServerError` | Retryable | `LLM_SERVER_ERROR` | HTTP 5xx |
| `LLMContextOverflowError` | Retryable | `LLM_CONTEXT_OVERFLOW` | context length exceeded |
| `LLMInvalidResponseError` | Fatal | `LLM_INVALID_RESPONSE` | malformed JSON / unparseable tool_calls |
| `LLMAuthError` | Fatal | `LLM_AUTH` | HTTP 401 / 403 |
| `LLMBadRequestError` | Fatal | `LLM_BAD_REQUEST` | HTTP 400 (excluding overflow) |

#### Tool Registry (ADR-0013)

| Class | Base | Code | When |
|---|---|---|---|
| `ToolNotFoundError` | Fatal | `TOOL_NOT_FOUND` | dispatching a tool that was never registered |
| `ToolArgumentValidationError` | (not raised; returned as ToolResult.error) | `TOOL_ARG_VALIDATION` | Pydantic input_model fails |
| `ToolExecutionError` | (not raised; returned as ToolResult.error) | `TOOL_EXEC` | tool raised internally |
| `ToolTimeoutError` | (not raised; returned as ToolResult.timeout) | `TOOL_TIMEOUT` | exceeded timeout_seconds |
| `MCPBridgeOfflineError` | Retryable | `MCP_BRIDGE_OFFLINE` | bridge subprocess disconnected mid-call |
| `MCPBridgeStartupError` | Fatal | `MCP_BRIDGE_STARTUP` | bridge failed to start during daemon init |

Note: tool errors are typically NOT raised. They become `ToolResult` entries that the LLM sees and reacts to. The exceptions are bridge failures (subprocess died unexpectedly) which surface up.

#### Storage (ADR-0019, planned)

| Class | Base | Code | When |
|---|---|---|---|
| `StoragePostgresUnavailable` | Retryable | `STORAGE_PG_UNAVAILABLE` | connection refused |
| `StorageNeo4jUnavailable` | Retryable | `STORAGE_NEO4J_UNAVAILABLE` | connection refused |
| `StorageIntegrityError` | Fatal | `STORAGE_INTEGRITY` | constraint violation (logic bug) |
| `StorageMigrationError` | Fatal | `STORAGE_MIGRATION` | Alembic migration failed |

#### Operation Logs (ADR-0005)

| Class | Base | Code | When |
|---|---|---|---|
| `OperationLogValidationError` | Fatal | `OPLOG_VALIDATION` | body missing required section, frontmatter invalid |
| `OperationLogIntegrityError` | Retryable | `OPLOG_INTEGRITY` | SHA-256 mismatch on read (file corruption) |
| `OperationLogConflictError` | Retryable | `OPLOG_CONFLICT` | UNIQUE constraint hit (multi-daemon race; v0.2 should not see this) |

#### Pipeline / Orchestrator (ADR-0003)

| Class | Base | Code | When |
|---|---|---|---|
| `WorkspaceNotFoundError` | Fatal | `WORKSPACE_NOT_FOUND` | cross-project workspace missing on disk |
| `UnknownProjectError` | Fatal | `UNKNOWN_PROJECT` | Jira project key not in mapping + no default |
| `PipelineStateInconsistencyError` | Fatal | `PIPELINE_STATE_INCONSISTENT` | Jira status + operation log out of agreement; needs human |
| `JiraTransitionForbiddenError` | Retryable | `JIRA_TRANSITION_FORBIDDEN` | service account lacks permission for a transition |
| `JiraTransitionInvalidError` | Retryable | `JIRA_TRANSITION_INVALID` | status changed under us between read and write |
| `StageRetryBudgetExhausted` | Fatal | `STAGE_RETRY_EXHAUSTED` | 3-strike limit hit; escalation expected |

#### Jira / GitHub clients

| Class | Base | Code | When |
|---|---|---|---|
| `JiraAuthError` | Fatal | `JIRA_AUTH` | 401 from Jira |
| `JiraNotFoundError` | (typically Retryable; depends on caller) | `JIRA_NOT_FOUND` | ticket missing |
| `JiraServerError` | Retryable | `JIRA_SERVER_ERROR` | 5xx |
| `GitHubAuthError` | Fatal | `GH_AUTH` | 401 / 403 |
| `GitHubNotFoundError` | Retryable | `GH_NOT_FOUND` | repo / Issue / PR missing |
| `GitHubServerError` | Retryable | `GH_SERVER_ERROR` | 5xx |
| `GitHubScopeError` | Fatal | `GH_SCOPE` | token lacks required scope |

#### Skill Loader (ADR-0012)

| Class | Base | Code | When |
|---|---|---|---|
| `SkillNotFoundError` | (not raised; returned as load_skill error result) | `SKILL_NOT_FOUND` | name not in index |
| `SkillToolsRequiredMissing` | (not raised; returned as load_skill error result) | `SKILL_TOOLS_MISSING` | tools_required not registered |
| `SkillParseError` | (logged during scan, skill omitted from index) | `SKILL_PARSE` | frontmatter / body invalid at load time |

#### Compactor (ADR-0011)

| Class | Base | Code | When |
|---|---|---|---|
| `OverPreservedError` | Retryable | `COMPACTOR_OVER_PRESERVED` | preservation rules conflict with size target |
| `CompactionSummarizationError` | Retryable | `COMPACTOR_SUMMARIZE_FAILED` | LLM summarization call failed; falls back to MicroCompact result |

#### Guardrail (ADR-0025, planned)

| Class | Base | Code | When |
|---|---|---|---|
| `GuardrailInputBlocked` | Fatal | `GUARD_INPUT_BLOCKED` | input guardrail rejected user message |
| `GuardrailOutputBlocked` | Fatal | `GUARD_OUTPUT_BLOCKED` | output guardrail rejected assistant message |
| `GuardrailActionRefusedByUser` | (not raised; returned as ToolResult.refused) | `GUARD_ACTION_REFUSED` | user declined confirmation |
| `GuardrailMisconfigured` | Fatal | `GUARD_MISCONFIGURED` | guardrail policy unparseable at startup |

#### Memory (ADR-0023, planned)

| Class | Base | Code | When |
|---|---|---|---|
| `MemoryWriteRejected` | (not raised; logged) | `MEMORY_WRITE_REJECTED` | governance filter rejected the write |
| `MemoryConflictUnresolved` | Retryable | `MEMORY_CONFLICT` | conflict detection wants human input |

#### Configuration (ADR-0016)

| Class | Base | Code | When |
|---|---|---|---|
| `ConfigValidationError` | Fatal | `CONFIG_VALIDATION` | pydantic-settings rejected at startup |
| `ConfigMissingFieldError` | Fatal | `CONFIG_MISSING` | required field not supplied |
| `ConfigFileUnreadable` | (warning; not fatal) | `CONFIG_FILE` | .env unreadable; continue with what's available |

### Propagation rules

#### Tool results (most errors stay here)

The Tool Registry catches almost everything inside `Tool.call()` and wraps it in `ToolResult` (ADR-0013). The Agent's LLM sees the error as a tool message and decides what to do. This prevents one bad tool call from halting the loop.

The exceptions (raised, NOT wrapped):

- `MCPBridgeStartupError` at daemon start → daemon refuses to start
- `MCPBridgeOfflineError` during dispatch → wrapped as `ToolResult.error` with `content="bridge offline"`
- `ToolNotFoundError` → wrapped as `ToolResult.error`

#### Agent Core (ADR-0009)

The Agent's loop catches:

- `RetryableError` from LLM → retry up to `rate_limit_retry_max` (default 3) with backoff
- `LLMContextOverflowError` → invoke Compactor, then retry once
- `FatalError` → halt the loop; `AgentResult.outcome="fatal_error"`
- `UserAbort` → halt; `AgentResult.outcome="user_abort"`

Tool errors come back as `ToolResult` entries; the loop continues.

#### Stage Handler

Handlers raise:

- `RetryableError` → orchestrator records as `outcome="failed"` and the next reaction event retries the stage (retry count from operation logs)
- `FatalError` → orchestrator records as `outcome="failed"`; if retry budget already exhausted → escalates immediately
- Tool result errors → handler decides whether to retry, escalate, or work around; the LLM does most of this through the Agent

#### Orchestrator

The Orchestrator:

- Counts retries via operation log queries
- Decides escalation when retry budget exhausted (raises nothing; writes ESCALATED log + transitions Jira label)
- Logs the AgentError chain (cause field) into the operation log's "What I could not do" section
- Catches `StorageIntegrityError` and similar from infrastructure → emits `critical` log + halts the daemon (these indicate bugs or data corruption)

#### Daemon

The Daemon's HTTP handlers + reaction loop catch:

- `ConfigValidationError` at startup → exit with code 2, stderr error
- `StoragePostgresUnavailable` / `StorageNeo4jUnavailable` at startup → exit with code 3
- Any uncaught `Exception` in a handler → log critical, send 500 to webhook caller (so Jira retries), continue serving
- `UserAbort` (SIGTERM) → graceful shutdown

### user_message conventions

Every error class declares a `user_message` template. When the error surfaces to a user (operation log, CLI, Jira comment), the `user_message` is shown — NOT the technical `str(self)`.

Examples:

```python
class JiraTransitionForbiddenError(RetryableError):
    code = "JIRA_TRANSITION_FORBIDDEN"
    user_message_template = (
        "The agent's service account does not have permission to transition "
        "this ticket from {from_status!r} to {to_status!r}. Ask a Jira admin "
        "to grant the transition to the {service_account!r} user."
    )

class LLMAuthError(FatalError):
    code = "LLM_AUTH"
    user_message_template = (
        "LLM provider returned 401/403. Check LLM_PRIMARY__API_KEY in your .env."
    )
```

The CLI's error output:

```
ERROR [LLM_AUTH]: LLM provider returned 401/403. Check LLM_PRIMARY__API_KEY in your .env.

Cause: openai.AuthenticationError: Incorrect API key provided
```

The Jira comment on escalation:

```
⚠️ Pipeline escalated: STAGE_RETRY_EXHAUSTED at stage `implement`.

3 attempts failed:
  1. [LLM_RATE_LIMIT] rate limited 3x; falling back to no-fallback config
  2. [TOOL_TIMEOUT] git_push timed out after 60s
  3. [LLM_CONTEXT_OVERFLOW] context exceeded after MicroCompact; reduce
     scope of this stage or split the ticket

Operation log: docs/operations/PROJ-123/02-implement-ESCALATED.md
```

### Logging

Every raised `AgentError` is logged with structured fields:

```python
logger.error(
    "agent error",
    code=exc.code,
    user_message=exc.user_message,
    base_class=type(exc).__mro__[1].__name__,    # RetryableError / FatalError / UserAbort
    cause=str(exc.cause) if exc.cause else None,
    cause_type=type(exc.cause).__name__ if exc.cause else None,
    **exc.context,
)
```

Subscribers (Dashboard, observability) aggregate by `code` for top-N error views.

### Error code stability

`code` strings are part of the public surface — operation logs reference them, metrics label them, downstream tools depend on them. Renaming a code is a breaking change. New error subclasses get new codes; deprecated codes are kept (or removed only with a major version bump).

A registry test asserts no two classes share a code.

### Translation table for `AgentResult.outcome` → `StageResult.outcome` (referenced from ADR-0003 + ADR-0009)

| AgentResult.outcome | error class (if any) | StageResult.outcome |
|---|---|---|
| completed | — | completed |
| max_turns_hit | (synthetic) | failed |
| max_tokens_hit | (synthetic) | failed |
| fatal_error | RetryableError-derived | failed |
| fatal_error | FatalError-derived | failed; orchestrator may escalate immediately if retry budget already at limit |
| user_abort | UserAbort | failed; no retry attempt |

### Testing

Unit tests for each error subclass:

- Assert `code` is set and unique
- Assert `user_message_template` renders without missing keys for representative context dicts
- Assert raise + catch with `isinstance(exc, RetryableError)` works

Integration tests verify propagation:

- Mock LLM returns 429 → Agent retries 3 times → on 4th 429 fails the turn → orchestrator records failure → next reaction retries the stage
- Mock tool raises → `ToolResult.error` → LLM gets a tool message → Agent loop continues

## Consequences

- Three-base taxonomy is shallow enough to reason about, deep enough at the leaves to act on (codes provide identification without inheritance gymnastics).
- Tool errors stay inside `ToolResult` rather than raising — the LLM owns recovery decisions, dramatically reducing fragile catch-all logic.
- Every error has a stable `code` and a `user_message_template`; CLI, Dashboard, Jira comments, and metrics all use these uniformly.
- Cause chaining preserves the underlying exception for debugging while the user-facing layer stays clean.
- Adding a new error is a small, well-defined change: subclass the right base, declare a code + template, register a test for uniqueness.

## Open Questions

| Q | Topic | Resolved in |
|---|---|---|
| Q1 | Localization of `user_message_template` (English-only in v0.2; Chinese-language teams may want translations) | Post-v0.2 |
| Q2 | Whether error codes participate in semantic versioning explicitly (e.g., codes prefixed with stability tier) | Phase 8 |
| Q3 | A "retry-after" hint surface that uses provider-suggested delays (already on `RetryableError.retry_after_seconds`; needs adapters to populate it) | Phase 2 implementation |
| Q4 | Error reporting telemetry — anonymized error code counters shipped to a project endpoint for triage | Post-v0.2; opt-in |

## References

- ADR-0009 Agent Core (consumes RetryableError, FatalError, UserAbort)
- ADR-0011 Compactor (OverPreservedError)
- ADR-0013 Tool Registry (ToolResult-wrapped errors + bridge errors)
- ADR-0014 LLM Adapter (LLM error mapping)
- ADR-0016 Configuration management (config errors)
- ADR-0019 Storage Layer (storage errors; planned)
- ADR-0023 Memory Governance (memory errors; planned)
- ADR-0025 Guardrail Layer (guardrail errors; planned)

## Reviewers

- [ ] Taven
