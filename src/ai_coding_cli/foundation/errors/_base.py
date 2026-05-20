"""Base classes for the AgentError taxonomy. See ADR-0017."""

from __future__ import annotations

from typing import Any, ClassVar


class AgentError(Exception):
    """Base for all errors in the agent runtime.

    Every concrete subclass MUST define a stable `code` class attribute. Codes
    are public surface — operation logs, metrics, and downstream tools consume
    them. Renaming a code is a breaking change.

    A `user_message_template` is rendered with the error's context dict when
    presented to a user (CLI, Dashboard, Jira comments). It's a regular
    `str.format` template (NOT f-string); fields are substituted from
    `self.context`.

    Subclasses do NOT have to override `__init__`. Construct like:

        raise LLMRateLimitError(
            "Rate limited by gpt-4o",
            cause=original_exc,
            retry_after_seconds=3.0,
            provider="openai-compat",
            model="gpt-4o",
        )

    Keyword arguments become `self.context`.
    """

    code: ClassVar[str] = "AGENT_ERROR"
    user_message_template: ClassVar[str | None] = None

    def __init__(
        self,
        message: str,
        *,
        cause: Exception | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.context: dict[str, Any] = context

    @property
    def user_message(self) -> str:
        """Render the user-friendly message. Falls back to `self.message`."""
        if self.user_message_template is None:
            return self.message
        try:
            return self.user_message_template.format(**self.context)
        except (KeyError, IndexError) as exc:
            # Missing context field; surface a degraded but useful message.
            return f"{self.message} (template render failed: {exc})"

    def __str__(self) -> str:  # pragma: no cover - thin format wrapper
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:  # pragma: no cover - thin format wrapper
        return f"{type(self).__name__}({self.code!r}, message={self.message!r})"


class RetryableError(AgentError):
    """Transient. The same call MAY succeed if retried, optionally after a delay."""

    code: ClassVar[str] = "RETRYABLE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        cause: Exception | None = None,
        retry_after_seconds: float | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, cause=cause, **context)
        self.retry_after_seconds = retry_after_seconds


class FatalError(AgentError):
    """Unrecoverable for the current invocation. Halts the loop / stage / handler."""

    code: ClassVar[str] = "FATAL_ERROR"


class UserAbort(AgentError):
    """User-initiated termination (SIGINT, Ctrl+C, daemon stop signal)."""

    code: ClassVar[str] = "USER_ABORT"
