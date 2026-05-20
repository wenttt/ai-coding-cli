"""Tests for the AgentError taxonomy. See ADR-0017."""

from __future__ import annotations

import pytest

from ai_coding_cli.foundation.errors import (
    ALL_ERROR_CLASSES,
    AgentError,
    ConfigMissingFieldError,
    ErrorCode,
    FatalError,
    JiraAuthError,
    LLMAuthError,
    LLMContextOverflowError,
    LLMRateLimitError,
    RetryableError,
    StorageMigrationError,
    UserAbort,
    check_code_uniqueness,
)


def test_base_classes_are_distinct() -> None:
    assert issubclass(RetryableError, AgentError)
    assert issubclass(FatalError, AgentError)
    assert issubclass(UserAbort, AgentError)
    assert not issubclass(RetryableError, FatalError)


def test_all_error_classes_have_unique_codes() -> None:
    """Importing errors module triggers this at runtime; explicit test for CI."""
    check_code_uniqueness()


def test_every_error_class_has_stable_code() -> None:
    for cls in ALL_ERROR_CLASSES:
        assert cls.code is not None
        assert cls.code != "", f"{cls.__name__} has empty code"
        assert isinstance(cls.code, str)


def test_retryable_error_carries_retry_after() -> None:
    exc = LLMRateLimitError(
        "Rate limited",
        provider="openai-compat",
        model="gpt-4o",
        retry_after_seconds=3.0,
    )
    assert exc.code == ErrorCode.LLM_RATE_LIMIT
    assert exc.retry_after_seconds == 3.0


def test_fatal_error_user_message_renders_template() -> None:
    exc = LLMAuthError("LLM auth", provider="openai-compat")
    assert "openai-compat" in exc.user_message
    assert exc.code == ErrorCode.LLM_AUTH


def test_user_message_template_missing_field_falls_back_gracefully() -> None:
    """If the context dict lacks a template variable, the user_message reports
    the failure rather than crashing."""
    exc = JiraAuthError("Jira auth", base_url="https://jira.test")
    msg = exc.user_message
    assert "jira.test" in msg


def test_user_message_missing_context_falls_back() -> None:
    exc = ConfigMissingFieldError("Missing JIRA_API_TOKEN")
    msg = exc.user_message
    # No context provided; template render fails -> degraded fallback.
    assert "Missing JIRA_API_TOKEN" in msg


def test_context_overflow_is_retryable() -> None:
    exc = LLMContextOverflowError("Overflow", model="gpt-4o")
    assert isinstance(exc, RetryableError)
    assert exc.code == ErrorCode.LLM_CONTEXT_OVERFLOW


def test_storage_migration_error_is_fatal() -> None:
    exc = StorageMigrationError("Migration failed", revision="0001_initial_lite")
    assert isinstance(exc, FatalError)
    assert "0001_initial_lite" in exc.user_message


def test_no_two_classes_share_a_code() -> None:
    """Explicit pairwise check (the registry already enforces this; belt + suspenders)."""
    codes: dict[str, type] = {}
    for cls in ALL_ERROR_CLASSES:
        if cls.code in codes:
            pytest.fail(
                f"Duplicate code {cls.code!r}: {cls.__name__} vs {codes[cls.code].__name__}"
            )
        codes[cls.code] = cls
