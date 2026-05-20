"""Concrete error classes. Each declares a stable `code` + user-message template."""

from __future__ import annotations

from typing import ClassVar

from ._base import FatalError, RetryableError
from ._codes import ErrorCode

# ---------------------------------------------------------------------------
# LLM Adapter (ADR-0014)
# ---------------------------------------------------------------------------


class LLMRateLimitError(RetryableError):
    code: ClassVar[str] = ErrorCode.LLM_RATE_LIMIT
    user_message_template: ClassVar[str | None] = (
        "LLM provider {provider!r} rate-limited the request "
        "(model={model!r}). Retry after {retry_after_seconds}s."
    )


class LLMTimeoutError(RetryableError):
    code: ClassVar[str] = ErrorCode.LLM_TIMEOUT
    user_message_template: ClassVar[str | None] = (
        "LLM call to {provider!r} timed out after {timeout_seconds}s "
        "(model={model!r})."
    )


class LLMServerError(RetryableError):
    code: ClassVar[str] = ErrorCode.LLM_SERVER_ERROR
    user_message_template: ClassVar[str | None] = (
        "LLM provider {provider!r} returned 5xx (model={model!r})."
    )


class LLMContextOverflowError(RetryableError):
    code: ClassVar[str] = ErrorCode.LLM_CONTEXT_OVERFLOW
    user_message_template: ClassVar[str | None] = (
        "Context window exceeded for model {model!r}. "
        "Compactor will retry after compaction."
    )


class LLMInvalidResponseError(FatalError):
    code: ClassVar[str] = ErrorCode.LLM_INVALID_RESPONSE
    user_message_template: ClassVar[str | None] = (
        "LLM provider {provider!r} returned an unparseable response. "
        "This usually means the model does not support the requested format."
    )


class LLMAuthError(FatalError):
    code: ClassVar[str] = ErrorCode.LLM_AUTH
    user_message_template: ClassVar[str | None] = (
        "LLM provider {provider!r} returned 401/403. "
        "Check LLM_PRIMARY__API_KEY in your .env."
    )


class LLMBadRequestError(FatalError):
    code: ClassVar[str] = ErrorCode.LLM_BAD_REQUEST


# ---------------------------------------------------------------------------
# Tool Registry (ADR-0013)
# ---------------------------------------------------------------------------


class ToolNotFoundError(FatalError):
    code: ClassVar[str] = ErrorCode.TOOL_NOT_FOUND
    user_message_template: ClassVar[str | None] = (
        "Tool {tool_name!r} is not registered. "
        "Check Tool Registry initialization or MCP bridge availability."
    )


class ToolArgumentValidationError(FatalError):
    """Raised at the dispatch-validation boundary. Typically wrapped into a
    ToolResult.error rather than propagated."""

    code: ClassVar[str] = ErrorCode.TOOL_ARG_VALIDATION


class ToolExecutionError(FatalError):
    """Raised inside Tool.call(). Typically wrapped into a ToolResult.error."""

    code: ClassVar[str] = ErrorCode.TOOL_EXEC


class ToolTimeoutError(FatalError):
    """Raised when a tool exceeds timeout_seconds. Wrapped into ToolResult.timeout."""

    code: ClassVar[str] = ErrorCode.TOOL_TIMEOUT


class MCPBridgeOfflineError(RetryableError):
    code: ClassVar[str] = ErrorCode.MCP_BRIDGE_OFFLINE
    user_message_template: ClassVar[str | None] = (
        "MCP bridge {bridge_name!r} is offline. Tool calls will return "
        "errors until the bridge reconnects."
    )


class MCPBridgeStartupError(FatalError):
    code: ClassVar[str] = ErrorCode.MCP_BRIDGE_STARTUP
    user_message_template: ClassVar[str | None] = (
        "MCP bridge {bridge_name!r} failed to start. "
        "Check the bridge command + args in your mcp_bridges.yaml."
    )


# ---------------------------------------------------------------------------
# Storage (ADR-0019)
# ---------------------------------------------------------------------------


class StorageSqliteUnavailable(RetryableError):
    code: ClassVar[str] = ErrorCode.STORAGE_SQLITE_UNAVAILABLE
    user_message_template: ClassVar[str | None] = (
        "SQLite database at {db_path!r} is unavailable. "
        "Check filesystem permissions + disk space."
    )


class StoragePostgresUnavailable(RetryableError):
    """Reserved for Standard profile (ADR-0019). Not emitted in Lite."""

    code: ClassVar[str] = ErrorCode.STORAGE_PG_UNAVAILABLE


class StorageIntegrityError(FatalError):
    code: ClassVar[str] = ErrorCode.STORAGE_INTEGRITY


class StorageMigrationError(FatalError):
    code: ClassVar[str] = ErrorCode.STORAGE_MIGRATION
    user_message_template: ClassVar[str | None] = (
        "Database migration failed at revision {revision!r}. "
        "Restore from backup and run `ai-coding migrate up`."
    )


# ---------------------------------------------------------------------------
# Operation Log (ADR-0005)
# ---------------------------------------------------------------------------


class OperationLogValidationError(FatalError):
    code: ClassVar[str] = ErrorCode.OPLOG_VALIDATION
    user_message_template: ClassVar[str | None] = (
        "Operation log body missing required section: {missing_section!r}."
    )


class OperationLogIntegrityError(RetryableError):
    code: ClassVar[str] = ErrorCode.OPLOG_INTEGRITY
    user_message_template: ClassVar[str | None] = (
        "Operation log file {file_path!r} SHA-256 mismatch. "
        "File may have been edited externally."
    )


class OperationLogConflictError(RetryableError):
    code: ClassVar[str] = ErrorCode.OPLOG_CONFLICT


# ---------------------------------------------------------------------------
# Pipeline / Orchestrator (ADR-0003)
# ---------------------------------------------------------------------------


class WorkspaceNotFoundError(FatalError):
    code: ClassVar[str] = ErrorCode.WORKSPACE_NOT_FOUND
    user_message_template: ClassVar[str | None] = (
        "Workspace path {workspace_path!r} does not exist or is not a directory."
    )


class UnknownProjectError(FatalError):
    code: ClassVar[str] = ErrorCode.UNKNOWN_PROJECT
    user_message_template: ClassVar[str | None] = (
        "Jira project {project_key!r} is not in your project_mapping.yaml + "
        "no default is configured."
    )


class PipelineStateInconsistencyError(FatalError):
    code: ClassVar[str] = ErrorCode.PIPELINE_STATE_INCONSISTENT


class JiraTransitionForbiddenError(RetryableError):
    code: ClassVar[str] = ErrorCode.JIRA_TRANSITION_FORBIDDEN
    user_message_template: ClassVar[str | None] = (
        "Agent service account lacks permission to transition ticket "
        "{ticket_key!r} from {from_status!r} to {to_status!r}. "
        "Ask a Jira admin to grant the transition."
    )


class JiraTransitionInvalidError(RetryableError):
    code: ClassVar[str] = ErrorCode.JIRA_TRANSITION_INVALID


class StageRetryBudgetExhausted(FatalError):
    code: ClassVar[str] = ErrorCode.STAGE_RETRY_EXHAUSTED
    user_message_template: ClassVar[str | None] = (
        "Stage {stage!r} on ticket {ticket_key!r} hit {max_retries} retries. "
        "Escalating to human review."
    )


# ---------------------------------------------------------------------------
# Jira / GitHub clients
# ---------------------------------------------------------------------------


class JiraAuthError(FatalError):
    code: ClassVar[str] = ErrorCode.JIRA_AUTH
    user_message_template: ClassVar[str | None] = (
        "Jira at {base_url!r} returned 401/403. "
        "Check JIRA_API_TOKEN in your .env."
    )


class JiraNotFoundError(RetryableError):
    code: ClassVar[str] = ErrorCode.JIRA_NOT_FOUND


class JiraServerError(RetryableError):
    code: ClassVar[str] = ErrorCode.JIRA_SERVER_ERROR


class GitHubAuthError(FatalError):
    code: ClassVar[str] = ErrorCode.GH_AUTH
    user_message_template: ClassVar[str | None] = (
        "GitHub at {base_url!r} returned 401/403. "
        "Check GITHUB_TOKEN and SSO authorization status."
    )


class GitHubNotFoundError(RetryableError):
    code: ClassVar[str] = ErrorCode.GH_NOT_FOUND


class GitHubServerError(RetryableError):
    code: ClassVar[str] = ErrorCode.GH_SERVER_ERROR


class GitHubScopeError(FatalError):
    code: ClassVar[str] = ErrorCode.GH_SCOPE
    user_message_template: ClassVar[str | None] = (
        "GitHub token lacks required scope {missing_scope!r}. "
        "Re-issue the token with the necessary permissions."
    )


# ---------------------------------------------------------------------------
# Guardrail (ADR-0025)
# ---------------------------------------------------------------------------


class GuardrailInputBlocked(FatalError):
    code: ClassVar[str] = ErrorCode.GUARD_INPUT_BLOCKED
    user_message_template: ClassVar[str | None] = (
        "Input guardrail blocked content matching {detected_signals!r}. "
        "Review the input source for prompt-injection patterns."
    )


class GuardrailOutputBlocked(FatalError):
    code: ClassVar[str] = ErrorCode.GUARD_OUTPUT_BLOCKED
    user_message_template: ClassVar[str | None] = (
        "Output guardrail blocked an assistant message containing "
        "{detected_signals!r}."
    )


class GuardrailActionRefusedByUser(FatalError):
    code: ClassVar[str] = ErrorCode.GUARD_ACTION_REFUSED


class GuardrailMisconfigured(FatalError):
    code: ClassVar[str] = ErrorCode.GUARD_MISCONFIGURED


# ---------------------------------------------------------------------------
# Configuration (ADR-0016)
# ---------------------------------------------------------------------------


class ConfigValidationError(FatalError):
    code: ClassVar[str] = ErrorCode.CONFIG_VALIDATION


class ConfigMissingFieldError(FatalError):
    code: ClassVar[str] = ErrorCode.CONFIG_MISSING
    user_message_template: ClassVar[str | None] = (
        "Required configuration field {field_name!r} is missing. "
        "See .env.example for the full list."
    )


class ConfigFileUnreadable(RetryableError):
    code: ClassVar[str] = ErrorCode.CONFIG_FILE
