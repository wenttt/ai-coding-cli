"""Smoke tests for native tools: schema generation + registry import.

Integration tests against real Jira / GitHub / git are in tests/integration/.
This file just verifies the @tool decorator wired everything up correctly.
"""

from __future__ import annotations

from ai_coding_cli.foundation.tools import global_registry


def test_native_tools_register_via_import() -> None:
    # Importing the native package triggers all @tool decorators.
    from ai_coding_cli.foundation.tools.native import (  # noqa: F401
        git_tools,
        github_tools,
        jira_tools,
        repo_tools,
        test_tools,
    )

    registry = global_registry()
    expected_jira = {
        "read_jira_ticket",
        "list_my_tickets",
        "create_jira_ticket",
        "add_jira_comment",
    }
    expected_github = {
        "create_design_issue",
        "update_design_issue",
        "get_issue_state",
        "list_issue_comments",
        "add_issue_comment",
        "close_issue",
        "find_design_issue_for_jira",
        "get_pr_state",
        "create_pr",
        "list_pr_review_comments",
    }
    expected_git = {
        "git_status",
        "git_diff",
        "git_log",
        "git_create_branch",
        "git_add",
        "git_commit",
        "git_push",
        "git_changed_files",
    }
    expected_repo = {
        "read_repo_file",
        "list_repo_files",
        "write_repo_file",
        "analyze_repo_state",
        "find_relevant_modules",
    }
    expected_tests = {
        "discover_test_framework",
        "discover_test_files",
        "run_tests",
    }

    all_names = {t.name for t in registry.all()}
    for expected in (expected_jira, expected_github, expected_git, expected_repo, expected_tests):
        missing = expected - all_names
        assert not missing, f"Native tools missing from registry: {missing}"


def test_native_tools_emit_valid_openai_schemas() -> None:
    from ai_coding_cli.foundation.tools.native import (  # noqa: F401
        git_tools,
        github_tools,
        jira_tools,
        repo_tools,
        test_tools,
    )

    registry = global_registry()
    schemas = registry.schemas_for_llm()
    for schema in schemas:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"]
        assert fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_orchestrator_only_tools_hidden_from_agent() -> None:
    from ai_coding_cli.foundation.tools.native import jira_tools  # noqa: F401

    registry = global_registry()
    schemas = registry.schemas_for_llm()
    schema_names = {s["function"]["name"] for s in schemas}
    # transition_jira_status is visible_to_agent=False per ADR-0013.
    assert "transition_jira_status" not in schema_names
    # It's still registered.
    assert registry.has("transition_jira_status")
