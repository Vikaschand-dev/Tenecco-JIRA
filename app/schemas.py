from typing import Any

from pydantic import BaseModel, Field

from app.adf import adf_to_text


class IssueCreate(BaseModel):
    project_key: str
    summary: str = Field(min_length=1, max_length=255)
    issue_type: str = Field("Task", description="Issue type name (e.g. 'Bug') or id")
    description: str | None = None
    priority: str | None = Field(None, description="Priority name or id")
    labels: list[str] | None = None
    assignee_account_id: str | None = None
    extra_fields: dict[str, Any] | None = Field(None, description="Raw Jira fields, e.g. custom fields")


class IssueUpdate(BaseModel):
    summary: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    priority: str | None = None
    labels: list[str] | None = None
    extra_fields: dict[str, Any] | None = None
    notify_users: bool = True


class StatusChange(BaseModel):
    status: str | None = Field(None, description="Target status name, e.g. 'In Progress' or 'Done'")
    transition_id: str | None = Field(None, description="Explicit transition id (takes precedence over status)")
    comment: str | None = None


class Assign(BaseModel):
    account_id: str | None = Field(None, description="Atlassian accountId; null to unassign")


class CommentIn(BaseModel):
    body: str = Field(min_length=1)


class SearchIn(BaseModel):
    jql: str
    max_results: int = Field(50, ge=1, le=100)
    next_page_token: str | None = None
    fields: list[str] | None = None


class WebhookSubscribe(BaseModel):
    jql_filter: str = Field(..., description="e.g. 'project = ABC' (limited JQL: project, issuetype, status, ...)")
    events: list[str] = Field(
        default_factory=lambda: [
            "jira:issue_created",
            "jira:issue_updated",
            "jira:issue_deleted",
            "comment_created",
            "comment_updated",
            "comment_deleted",
        ]
    )


def _user(u: dict | None) -> dict | None:
    if not u:
        return None
    return {"account_id": u.get("accountId"), "display_name": u.get("displayName"), "avatar": (u.get("avatarUrls") or {}).get("24x24")}


def simplify_issue(issue: dict) -> dict:
    """Flatten a Jira issue into a compact, platform-friendly shape."""
    f = issue.get("fields", {}) or {}
    status = f.get("status") or {}
    return {
        "id": issue.get("id"),
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "description": adf_to_text(f.get("description")) if "description" in f else None,
        "status": status.get("name"),
        "status_category": (status.get("statusCategory") or {}).get("key"),
        "issue_type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": _user(f.get("assignee")),
        "reporter": _user(f.get("reporter")),
        "labels": f.get("labels") or [],
        "parent": (f.get("parent") or {}).get("key"),
        "project": (f.get("project") or {}).get("key"),
        "created": f.get("created"),
        "updated": f.get("updated"),
    }


def simplify_comment(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "author": _user(c.get("author")),
        "body": adf_to_text(c.get("body")),
        "created": c.get("created"),
        "updated": c.get("updated"),
    }
