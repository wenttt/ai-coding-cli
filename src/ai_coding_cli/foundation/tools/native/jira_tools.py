"""Native Jira tools. See ADR-0013 §Jira."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .._context import ToolContext
from .._decorator import tool
from .._side_effects import SideEffectClass
from ._clients import get_jira_client

# ---------------------------------------------------------------------------
# read_jira_ticket
# ---------------------------------------------------------------------------


class ReadJiraTicketArgs(BaseModel):
    jira_key: str = Field(description="The Jira ticket key, e.g. 'PROJ-123'.")


@tool(
    name="read_jira_ticket",
    description="Read a Jira ticket and return its essential fields: key, summary, description, status, ticket_type, labels, assignee, components, linked_issues, url.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def read_jira_ticket(args: ReadJiraTicketArgs, ctx: ToolContext) -> dict[str, Any]:
    client = get_jira_client(ctx.config)
    issue = client.issue(args.jira_key)
    return _summarize_issue(issue)


# ---------------------------------------------------------------------------
# list_my_tickets
# ---------------------------------------------------------------------------


class ListMyTicketsArgs(BaseModel):
    status: str | None = Field(
        default="In Progress",
        description="Jira status filter; pass null for any status.",
    )
    limit: int = Field(default=20, ge=1, le=100)


@tool(
    name="list_my_tickets",
    description="List Jira tickets assigned to the configured user, optionally filtered by status.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def list_my_tickets(args: ListMyTicketsArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    client = get_jira_client(ctx.config)
    assignee = ctx.config.jira.email or "currentUser()"
    jql_parts = [f'assignee = "{assignee}"'] if "@" in assignee else [f"assignee = {assignee}"]
    if args.status:
        jql_parts.append(f'status = "{args.status}"')
    jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"
    result = client.jql(jql, limit=args.limit)
    return [_summarize_issue(i) for i in result.get("issues", [])]


# ---------------------------------------------------------------------------
# create_jira_ticket
# ---------------------------------------------------------------------------


class CreateJiraTicketArgs(BaseModel):
    project_key: str
    summary: str
    description: str = ""
    issue_type: str = Field(
        default="Task",
        description="Jira issue type name (Task / Story / Bug / Sub-task / Epic).",
    )
    labels: list[str] = Field(default_factory=list)
    parent_key: str | None = Field(
        default=None,
        description="Parent ticket key for sub-tasks.",
    )


@tool(
    name="create_jira_ticket",
    description="Create a new Jira ticket. Use parent_key for sub-tasks.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def create_jira_ticket(args: CreateJiraTicketArgs, ctx: ToolContext) -> dict[str, Any]:
    client = get_jira_client(ctx.config)
    fields: dict[str, Any] = {
        "project": {"key": args.project_key},
        "summary": args.summary,
        "description": args.description,
        "issuetype": {"name": args.issue_type},
    }
    if args.labels:
        fields["labels"] = list(args.labels)
    if args.parent_key:
        fields["parent"] = {"key": args.parent_key}
    issue = client.issue_create(fields=fields)
    return {
        "key": issue.get("key"),
        "id": issue.get("id"),
        "url": _browse_url(ctx, issue.get("key")),
    }


# ---------------------------------------------------------------------------
# transition_jira_status
# ---------------------------------------------------------------------------


class TransitionJiraStatusArgs(BaseModel):
    jira_key: str
    target_status: str = Field(
        description="The target status name as defined by the project's workflow."
    )


@tool(
    name="transition_jira_status",
    description="Transition a Jira ticket to a new status. The Orchestrator is the typical caller; this tool is hidden from the agent.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
    visible_to_agent=False,  # orchestrator-only
)
def transition_jira_status(
    args: TransitionJiraStatusArgs, ctx: ToolContext
) -> dict[str, Any]:
    client = get_jira_client(ctx.config)
    before = client.issue(args.jira_key)
    previous = (before.get("fields", {}).get("status") or {}).get("name", "")
    client.set_issue_status(args.jira_key, args.target_status)
    after = client.issue(args.jira_key)
    return {
        "key": args.jira_key,
        "previous_status": previous,
        "new_status": (after.get("fields", {}).get("status") or {}).get("name", ""),
    }


# ---------------------------------------------------------------------------
# add_jira_comment
# ---------------------------------------------------------------------------


class AddJiraCommentArgs(BaseModel):
    jira_key: str
    body: str


@tool(
    name="add_jira_comment",
    description="Post a comment on a Jira ticket. Use for escalation notifications + cross-references.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def add_jira_comment(args: AddJiraCommentArgs, ctx: ToolContext) -> dict[str, Any]:
    client = get_jira_client(ctx.config)
    result = client.issue_add_comment(args.jira_key, args.body)
    return {"jira_key": args.jira_key, "comment_id": result.get("id")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "story": "user_story",
    "user story": "user_story",
    "task": "task",
    "sub-task": "sub_task",
    "subtask": "sub_task",
    "epic": "epic",
    "bug": "task",
}


def _summarize_issue(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields", {})
    issuetype = (fields.get("issuetype") or {}).get("name", "").lower()
    key = issue.get("key", "")
    project_key = key.split("-", 1)[0].upper() if "-" in key else key.upper()
    parent = fields.get("parent") or {}
    return {
        "key": key,
        "project_key": project_key,
        "parent_key": parent.get("key"),
        "summary": fields.get("summary", ""),
        "description": fields.get("description") or "",
        "status": (fields.get("status") or {}).get("name", ""),
        "ticket_type": _TYPE_MAP.get(issuetype, "task"),
        "raw_issuetype": issuetype,
        "labels": fields.get("labels") or [],
        "assignee": (
            (fields.get("assignee") or {}).get("emailAddress")
            or (fields.get("assignee") or {}).get("displayName")
        ),
        "priority": (fields.get("priority") or {}).get("name"),
        "components": [c.get("name") for c in (fields.get("components") or [])],
        "linked_issues": [
            link.get("outwardIssue", link.get("inwardIssue", {})).get("key")
            for link in (fields.get("issuelinks") or [])
        ],
        "url": (
            f"{issue.get('self', '').split('/rest/')[0]}/browse/{key}"
            if issue.get("self")
            else None
        ),
    }


def _browse_url(ctx: ToolContext, key: str | None) -> str | None:
    if key is None:
        return None
    return f"{str(ctx.config.jira.base_url).rstrip('/')}/browse/{key}"
