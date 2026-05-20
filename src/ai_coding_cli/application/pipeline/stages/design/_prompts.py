"""User-message prompts for the Design stage. See ADR-0004."""

from __future__ import annotations

from typing import Any

BROWNFIELD_DESIGN_PROMPT = """\
You are running Stage 1 (Design) for Jira ticket {jira_key}.

## Ticket

- Summary: {summary}
- Type: {ticket_type}
- Status: {status}
- Description:

{description}

## Your task

1. **Check idempotency.** Call `find_design_issue_for_jira` with `jira_key={jira_key}`.
   If an open design Issue already exists, fetch its current body and reuse it as
   the base; otherwise plan a fresh design.

2. **Gather repo context.** Extract 3-7 concrete keywords from the summary +
   description (skip generic words like "implement", "add", "feature"). Call
   `find_relevant_modules` with those keywords. Then `read_repo_file` on the
   top 3-5 results, capping each at 8 KB.

3. **Compose the design.** Produce a Markdown body with these sections in this
   order:

   ```
   ---
   jira_key: {jira_key}
   mode: brownfield
   ticket_type: {ticket_type}
   risk_level: low|medium|high
   ac:
     - "..."
   affected_modules:
     - "..."
   ---

   # Design: <summary>

   ## Background
   ## Story / Goal
   ## Acceptance criteria
   ## Affected modules
   ## Design
   ## Test plan
   ## Open questions
   ```

4. **Publish.** Call `create_design_issue` (or `update_design_issue` if step 1
   found one) with the body. Title format: `[{jira_key}] Design: <summary>`.
   Labels: `jira:{jira_key_lower}`, `stage:design`, `mode:brownfield`,
   `ticket-type:{ticket_type}`.

5. **Notify Jira.** Call `add_jira_comment` with a short link to the design
   Issue URL.

6. **Finish.** Return a final assistant message in this exact shape (no tool
   calls):

   ```
   STAGE_RESULT
   outcome: completed
   design_issue_url: <url>
   design_issue_number: <number>
   risk_level: <low|medium|high>
   summary: <one-line summary of what you decided>
   ```

   If you could not produce a valid design (e.g. the ticket is too vague to
   draft acceptance criteria), set `outcome: failed` and explain in `summary`.

## Constraints

- This stage is **Jira-Issue-only**: do NOT touch the repo working tree.
  No `write_repo_file`, no git operations, no `run_tests`.
- Open questions go in the Issue body's `## Open questions` section. Do not
  leave silent gaps.
- Cap your loop at the budget your runtime enforces; if you hit it, return
  `outcome: failed` with the partial state described.
"""


def build_brownfield_user_message(ticket: dict[str, Any]) -> str:
    """Render the brownfield prompt with the ticket's canonical fields."""
    return BROWNFIELD_DESIGN_PROMPT.format(
        jira_key=ticket.get("key", "UNKNOWN"),
        summary=ticket.get("summary", ""),
        ticket_type=ticket.get("ticket_type", "user_story"),
        status=ticket.get("status", "DESIGN_DRAFTING"),
        description=(ticket.get("description") or "").strip() or "(no description)",
        jira_key_lower=str(ticket.get("key", "UNKNOWN")).lower(),
    )
