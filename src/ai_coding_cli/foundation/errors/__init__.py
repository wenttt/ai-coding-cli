"""Agent error taxonomy.

Three base classes:

- RetryableError: transient; the same call may succeed if retried.
- FatalError: unrecoverable for the current Agent invocation; halt the loop.
- UserAbort: user-initiated termination; no retry.

Concrete leaves carry a stable `code` string for identification by external
consumers (logs, metrics, Dashboard, operation logs). Renaming a code is a
breaking change. See ADR-0017 for the full mapping.
"""

from __future__ import annotations

from ._base import AgentError, FatalError, RetryableError, UserAbort
from ._codes import ErrorCode
from ._leaves import (
    ConfigFileUnreadable,
    ConfigMissingFieldError,
    ConfigValidationError,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubScopeError,
    GitHubServerError,
    GuardrailActionRefusedByUser,
    GuardrailInputBlocked,
    GuardrailMisconfigured,
    GuardrailOutputBlocked,
    JiraAuthError,
    JiraNotFoundError,
    JiraServerError,
    JiraTransitionForbiddenError,
    JiraTransitionInvalidError,
    LLMAuthError,
    LLMBadRequestError,
    LLMContextOverflowError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    MCPBridgeOfflineError,
    MCPBridgeStartupError,
    OperationLogConflictError,
    OperationLogIntegrityError,
    OperationLogValidationError,
    PipelineStateInconsistencyError,
    StageRetryBudgetExhausted,
    StorageIntegrityError,
    StorageMigrationError,
    StoragePostgresUnavailable,
    StorageSqliteUnavailable,
    ToolArgumentValidationError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolTimeoutError,
    UnknownProjectError,
    WorkspaceNotFoundError,
)
from ._registry import ALL_ERROR_CLASSES, check_code_uniqueness

__all__ = [
    # bases
    "AgentError",
    "RetryableError",
    "FatalError",
    "UserAbort",
    "ErrorCode",
    # LLM
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMServerError",
    "LLMContextOverflowError",
    "LLMInvalidResponseError",
    "LLMAuthError",
    "LLMBadRequestError",
    # Tools
    "ToolNotFoundError",
    "ToolArgumentValidationError",
    "ToolExecutionError",
    "ToolTimeoutError",
    "MCPBridgeOfflineError",
    "MCPBridgeStartupError",
    # Storage
    "StorageSqliteUnavailable",
    "StoragePostgresUnavailable",
    "StorageIntegrityError",
    "StorageMigrationError",
    # Operation logs
    "OperationLogValidationError",
    "OperationLogIntegrityError",
    "OperationLogConflictError",
    # Pipeline
    "WorkspaceNotFoundError",
    "UnknownProjectError",
    "PipelineStateInconsistencyError",
    "JiraTransitionForbiddenError",
    "JiraTransitionInvalidError",
    "StageRetryBudgetExhausted",
    # Jira / GitHub
    "JiraAuthError",
    "JiraNotFoundError",
    "JiraServerError",
    "GitHubAuthError",
    "GitHubNotFoundError",
    "GitHubServerError",
    "GitHubScopeError",
    # Guardrails
    "GuardrailInputBlocked",
    "GuardrailOutputBlocked",
    "GuardrailActionRefusedByUser",
    "GuardrailMisconfigured",
    # Config
    "ConfigValidationError",
    "ConfigMissingFieldError",
    "ConfigFileUnreadable",
    # Registry
    "ALL_ERROR_CLASSES",
    "check_code_uniqueness",
]
