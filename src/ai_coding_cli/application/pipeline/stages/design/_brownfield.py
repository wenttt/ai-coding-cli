"""BrownfieldDesignHandler. See ADR-0004 §Brownfield design flow."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai_coding_cli.foundation.agent import AgentOutcome

from ....operation_log import OperationLogBody
from ..._context import StageContext, StageResult
from ._prompts import build_brownfield_user_message

logger = logging.getLogger(__name__)


_STAGE_RESULT_RE = re.compile(
    r"STAGE_RESULT\s*(?P<body>(?:.+\n?)*)", re.MULTILINE
)


class BrownfieldDesignHandler:
    """Concrete handler for brownfield design tickets.

    The handler runs the Agent with a structured prompt. The Agent makes
    tool calls to read the repo + create the Design Issue, then returns a
    final message that the handler parses into a StageResult.
    """

    stage_name = "design"
    entry_status = "DESIGN_DRAFTING"
    exit_status_on_success = "DESIGN_REVIEW"
    exit_status_on_failure = "DESIGN_DRAFTING"
    max_retries = 3

    async def run(self, ctx: StageContext) -> StageResult:
        user_message = build_brownfield_user_message(ctx.jira_ticket)
        result = await ctx.agent.run(user_message)

        artifacts: dict[str, str] = {}
        parsed = _parse_stage_result(result.final_assistant_message or "")
        if parsed:
            if url := parsed.get("design_issue_url"):
                artifacts["design_issue_url"] = url
            if number := parsed.get("design_issue_number"):
                artifacts["design_issue_number"] = str(number)
            if risk := parsed.get("risk_level"):
                artifacts["risk_level"] = risk

        # Also scan the conversation for create_design_issue tool results
        # as a fallback when the LLM didn't structure its final message well.
        if "design_issue_url" not in artifacts:
            fallback = await _scan_design_issue_from_session(ctx)
            artifacts.update(fallback)

        outcome = _determine_outcome(result.outcome, parsed, artifacts)
        summary = _build_summary(parsed, result, artifacts)
        body = _build_operation_log_body(
            ctx=ctx,
            agent_final_message=result.final_assistant_message,
            artifacts=artifacts,
            outcome=outcome,
            summary=summary,
        )

        return StageResult(
            outcome=outcome,
            summary=summary,
            artifacts=artifacts,
            body=body,
        )


# ---------------------------------------------------------------------------
# Parsers + helpers
# ---------------------------------------------------------------------------


def _parse_stage_result(message: str) -> dict[str, str]:
    """Extract `STAGE_RESULT key: value` lines from the agent's final message."""
    if "STAGE_RESULT" not in message:
        return {}
    body = message.split("STAGE_RESULT", 1)[1]
    out: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().lstrip("`").rstrip("`").strip()
        if key and value and value != "<url>":
            out[key] = value
    return out


async def _scan_design_issue_from_session(ctx: StageContext) -> dict[str, str]:
    """Re-read the persisted Conversation and look for create_design_issue
    tool results. Returns the URL + number if found.
    """
    fresh = await ctx.agent._session_manager.get_conversation(  # noqa: SLF001
        ctx.conversation.id
    )
    if fresh is None:
        return {}

    out: dict[str, str] = {}
    for msg in fresh.messages:
        if msg.role != "tool":
            continue
        if msg.name not in ("create_design_issue", "update_design_issue"):
            continue
        try:
            payload = json.loads(msg.content)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            if url := payload.get("html_url") or payload.get("url"):
                out["design_issue_url"] = url
            if number := payload.get("number"):
                out["design_issue_number"] = str(number)
    return out


def _determine_outcome(
    agent_outcome: AgentOutcome,
    parsed: dict[str, str],
    artifacts: dict[str, str],
) -> str:
    """Translate the Agent's outcome + the parsed result into a stage outcome."""
    if agent_outcome == AgentOutcome.FATAL_ERROR:
        return "failed"
    if agent_outcome == AgentOutcome.USER_ABORT:
        return "failed"
    if agent_outcome in (AgentOutcome.MAX_TURNS_HIT, AgentOutcome.MAX_TOKENS_HIT):
        return "failed"

    declared_outcome = parsed.get("outcome", "").lower()
    if declared_outcome in ("failed", "escalated"):
        return declared_outcome

    if "design_issue_url" not in artifacts:
        return "failed"

    return "completed"


def _build_summary(
    parsed: dict[str, str],
    agent_result: Any,
    artifacts: dict[str, str],
) -> str:
    if summary := parsed.get("summary"):
        return summary
    if artifacts.get("design_issue_url"):
        return (
            f"Design Issue created at {artifacts['design_issue_url']}; "
            "ready for review."
        )
    return "Design stage did not produce a Design Issue."


def _build_operation_log_body(
    *,
    ctx: StageContext,
    agent_final_message: str | None,
    artifacts: dict[str, str],
    outcome: str,
    summary: str,
) -> OperationLogBody:
    has_issue = "design_issue_url" in artifacts

    what_was_done_lines = [
        f"- Read Jira ticket {ctx.jira_key} ({ctx.jira_ticket.get('ticket_type', 'unknown')}).",
        "- Searched repository for context modules.",
    ]
    if has_issue:
        what_was_done_lines.append(
            f"- Created/updated Design Issue: {artifacts['design_issue_url']}"
        )
    else:
        what_was_done_lines.append("- Did NOT produce a Design Issue (see 'What I could not do').")

    impact_lines: list[str] = []
    if has_issue:
        impact_lines.append(
            "- Design Issue available for reviewer feedback at "
            f"{artifacts['design_issue_url']}."
        )
        if risk := artifacts.get("risk_level"):
            impact_lines.append(f"- Risk level: {risk}.")
    else:
        impact_lines.append("- No artifact produced; ticket remains in DESIGN_DRAFTING.")

    what_i_could_not_do_lines: list[str] = []
    if not has_issue:
        what_i_could_not_do_lines.append(
            "- Did not create a Design Issue. The agent loop terminated without "
            "publishing one."
        )
    if outcome == "failed":
        what_i_could_not_do_lines.append(f"- Outcome was failed: {summary}")
    if not what_i_could_not_do_lines:
        what_i_could_not_do_lines.append("- _(none)_")

    engineering_decisions_lines = [
        f"- Treated ticket as **brownfield** mode (workspace: {ctx.workspace_root}).",
    ]
    if has_issue:
        engineering_decisions_lines.append(
            "- See the Design Issue body for design-level decisions (architecture, "
            "data flow, error handling)."
        )
    else:
        engineering_decisions_lines.append("- _(stage did not complete; no decisions recorded)_")

    next_step_lines: list[str]
    if outcome == "completed":
        next_step_lines = [
            "- Reviewers should evaluate the Design Issue and either approve "
            "(transitioning the ticket to IN_DEVELOPMENT) or request changes "
            "(DESIGN_REVIEW → DESIGN_REWORK).",
        ]
    else:
        next_step_lines = [
            "- The next event on this ticket will re-enter the design stage "
            f"(attempt {ctx.retry_count + 2}).",
        ]

    return OperationLogBody(
        what_was_done="\n".join(what_was_done_lines),
        impact="\n".join(impact_lines),
        what_i_could_not_do="\n".join(what_i_could_not_do_lines),
        engineering_decisions="\n".join(engineering_decisions_lines),
        next_step="\n".join(next_step_lines),
    )
