"""Native GitHub tools. See ADR-0013 §GitHub."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .._context import ToolContext
from .._decorator import tool
from .._side_effects import SideEffectClass
from ._clients import get_github_client


def _repo_or_default(ctx: ToolContext, owner: str | None, repo: str | None) -> Any:
    gh = get_github_client(ctx.config)
    o = owner or ctx.config.github.default_owner
    r = repo or ctx.config.github.default_repo
    if not o or not r:
        raise ValueError(
            "GitHub owner and repo must be specified, either explicitly or via "
            "GITHUB_DEFAULT_OWNER + GITHUB_DEFAULT_REPO env vars."
        )
    return gh.get_repo(f"{o}/{r}")


# ---------------------------------------------------------------------------
# Issues (used heavily by Stage 1 design)
# ---------------------------------------------------------------------------


class CreateDesignIssueArgs(BaseModel):
    jira_key: str
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
    owner: str | None = None
    repo: str | None = None


@tool(
    name="create_design_issue",
    description="Open a GitHub Issue containing the design markdown (Stage 1).",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def create_design_issue(args: CreateDesignIssueArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    default_labels = [f"jira:{args.jira_key.lower()}", "stage:design"]
    all_labels = list(set(default_labels + list(args.labels)))
    issue = gh_repo.create_issue(title=args.title, body=args.body, labels=all_labels)
    return {
        "number": issue.number,
        "url": issue.html_url,
        "state": issue.state,
        "labels": [lbl.name for lbl in issue.labels],
    }


class UpdateDesignIssueArgs(BaseModel):
    issue_number: int
    body: str
    owner: str | None = None
    repo: str | None = None


@tool(
    name="update_design_issue",
    description="Replace the body of a design Issue (used by mcp-design-revise).",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def update_design_issue(args: UpdateDesignIssueArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    issue = gh_repo.get_issue(args.issue_number)
    issue.edit(body=args.body)
    issue = gh_repo.get_issue(args.issue_number)
    return {
        "number": issue.number,
        "url": issue.html_url,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
    }


class GetIssueStateArgs(BaseModel):
    issue_number: int
    owner: str | None = None
    repo: str | None = None


@tool(
    name="get_issue_state",
    description="Read the current state of a GitHub Issue.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def get_issue_state(args: GetIssueStateArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    issue = gh_repo.get_issue(args.issue_number)
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "state": issue.state,
        "state_reason": issue.state_reason,
        "labels": [lbl.name for lbl in issue.labels],
        "assignees": [a.login for a in issue.assignees],
        "comments_count": issue.comments,
        "url": issue.html_url,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
    }


class ListIssueCommentsArgs(BaseModel):
    issue_number: int
    owner: str | None = None
    repo: str | None = None


@tool(
    name="list_issue_comments",
    description="List comments on a GitHub Issue.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def list_issue_comments(args: ListIssueCommentsArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    issue = gh_repo.get_issue(args.issue_number)
    out = []
    for comment in issue.get_comments():
        out.append({
            "id": comment.id,
            "author": comment.user.login if comment.user else None,
            "body": comment.body or "",
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
            "url": comment.html_url,
        })
    return sorted(out, key=lambda c: c["created_at"] or "")


class AddIssueCommentArgs(BaseModel):
    issue_number: int
    body: str
    owner: str | None = None
    repo: str | None = None


@tool(
    name="add_issue_comment",
    description="Post a comment on a GitHub Issue.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def add_issue_comment(args: AddIssueCommentArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    issue = gh_repo.get_issue(args.issue_number)
    comment = issue.create_comment(args.body)
    return {"id": comment.id, "url": comment.html_url}


class CloseIssueArgs(BaseModel):
    issue_number: int
    state_reason: str = Field(default="completed", description="'completed' or 'not_planned'.")
    owner: str | None = None
    repo: str | None = None


@tool(
    name="close_issue",
    description="Close a GitHub Issue. state_reason='completed' = approved; 'not_planned' = rejected outright.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def close_issue(args: CloseIssueArgs, ctx: ToolContext) -> dict[str, Any]:
    if args.state_reason not in {"completed", "not_planned"}:
        raise ValueError(
            f"state_reason must be 'completed' or 'not_planned', got {args.state_reason!r}"
        )
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    issue = gh_repo.get_issue(args.issue_number)
    issue.edit(state="closed", state_reason=args.state_reason)
    return {"number": issue.number, "state": "closed", "state_reason": args.state_reason}


class FindDesignIssueArgs(BaseModel):
    jira_key: str
    owner: str | None = None
    repo: str | None = None


@tool(
    name="find_design_issue_for_jira",
    description="Find the design Issue for a Jira ticket by label. Returns null if not found.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def find_design_issue_for_jira(args: FindDesignIssueArgs, ctx: ToolContext) -> dict[str, Any] | None:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    jira_label = f"jira:{args.jira_key.lower()}"
    issues = gh_repo.get_issues(state="all", labels=[jira_label, "stage:design"])
    for issue in issues:
        return {
            "number": issue.number,
            "url": issue.html_url,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "title": issue.title,
        }
    return None


# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------


class GetPrStateArgs(BaseModel):
    pr_number: int
    owner: str | None = None
    repo: str | None = None


@tool(
    name="get_pr_state",
    description="Read the current state of a GitHub PR (number, title, body, state, mergeable, review_decision, etc).",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def get_pr_state(args: GetPrStateArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    pr = gh_repo.get_pull(args.pr_number)
    return _summarize_pr(pr)


class CreatePrArgs(BaseModel):
    title: str
    body: str
    head_branch: str
    base_branch: str = "main"
    labels: list[str] = Field(default_factory=list)
    draft: bool = False
    owner: str | None = None
    repo: str | None = None


@tool(
    name="create_pr",
    description="Open a new PR. The head branch must already exist on the remote.",
    side_effects=SideEffectClass.EXTERNAL_WRITE,
)
def create_pr(args: CreatePrArgs, ctx: ToolContext) -> dict[str, Any]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    pr = gh_repo.create_pull(
        title=args.title,
        body=args.body,
        head=args.head_branch,
        base=args.base_branch,
        draft=args.draft,
    )
    if args.labels:
        pr.set_labels(*args.labels)
    return _summarize_pr(pr)


class ListPrReviewCommentsArgs(BaseModel):
    pr_number: int
    owner: str | None = None
    repo: str | None = None


@tool(
    name="list_pr_review_comments",
    description="List review comments + general review bodies on a PR.",
    side_effects=SideEffectClass.EXTERNAL_READ,
)
def list_pr_review_comments(args: ListPrReviewCommentsArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    gh_repo = _repo_or_default(ctx, args.owner, args.repo)
    pr = gh_repo.get_pull(args.pr_number)
    out = []
    for review in pr.get_reviews():
        if review.body and review.body.strip():
            out.append({
                "kind": "review_summary",
                "author": review.user.login if review.user else None,
                "body": review.body,
                "state": review.state,
                "created_at": review.submitted_at.isoformat() if review.submitted_at else None,
                "path": None,
                "line": None,
            })
    for comment in pr.get_review_comments():
        out.append({
            "kind": "line_comment",
            "author": comment.user.login if comment.user else None,
            "body": comment.body,
            "state": None,
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
            "path": comment.path,
            "line": comment.line,
        })
    return sorted(out, key=lambda c: c["created_at"] or "")


def _summarize_pr(pr: Any) -> dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "body": pr.body or "",
        "state": pr.state,
        "merged": pr.merged,
        "mergeable": pr.mergeable,
        "draft": pr.draft,
        "head_ref": pr.head.ref,
        "base_ref": pr.base.ref,
        "author": pr.user.login,
        "url": pr.html_url,
        "labels": [lbl.name for lbl in pr.get_labels()],
        "review_decision": _get_review_decision(pr),
    }


def _get_review_decision(pr: Any) -> str:
    latest_per_reviewer: dict[str, str] = {}
    for review in pr.get_reviews():
        if review.user is None:
            continue
        if review.state in {"APPROVED", "CHANGES_REQUESTED"}:
            latest_per_reviewer[review.user.login] = review.state
    if not latest_per_reviewer:
        return "no_review_yet"
    if "CHANGES_REQUESTED" in latest_per_reviewer.values():
        return "changes_requested"
    if all(state == "APPROVED" for state in latest_per_reviewer.values()):
        return "approved"
    return "pending"
