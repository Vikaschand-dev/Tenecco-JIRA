from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.deps import get_jira
from app.jira_client import JiraClient
from app.schemas import (
    Assign,
    CommentIn,
    IssueCreate,
    IssueUpdate,
    SearchIn,
    StatusChange,
    simplify_comment,
    simplify_issue,
)

router = APIRouter(prefix="/api/jira", tags=["jira"])


# ---------------------------------------------------------------- metadata


@router.get("/me")
async def me(jira: JiraClient = Depends(get_jira)):
    u = await jira.myself()
    return {"account_id": u.get("accountId"), "display_name": u.get("displayName"), "email": u.get("emailAddress")}


@router.get("/projects")
async def projects(query: str | None = None, jira: JiraClient = Depends(get_jira)):
    items = await jira.list_projects(query=query)
    return [{"id": p["id"], "key": p["key"], "name": p["name"]} for p in items]


@router.get("/projects/{project_key}/issue-types")
async def issue_types(project_key: str, jira: JiraClient = Depends(get_jira)):
    items = await jira.project_issue_types(project_key)
    return [{"id": t["id"], "name": t["name"], "subtask": t.get("subtask", False)} for t in items]


CATEGORY_ORDER = {"new": 0, "indeterminate": 1, "done": 2}
BOARD_FIELDS = ["summary", "status", "assignee", "priority", "issuetype", "labels", "parent", "updated", "created"]


@router.get("/projects/{project_key}/board")
async def board(
    project_key: str,
    jql: str | None = Query(None, description="Extra JQL filter, e.g. assignee = currentUser()"),
    jira: JiraClient = Depends(get_jira),
):
    """Kanban data: one column per workflow status, with the project's issues."""
    statuses = await jira.project_statuses(project_key)
    columns = sorted(
        (
            {"id": s["id"], "name": s["name"], "category": (s.get("statusCategory") or {}).get("key", "indeterminate")}
            for s in statuses
        ),
        # Jira exposes no column order for business boards: order by category, with
        # "In Progress"-style statuses first among the in-flight ones. The UI lets users reorder.
        key=lambda c: (CATEGORY_ORDER.get(c["category"], 1), "progress" not in c["name"].lower()),
    )
    where = f'project = "{project_key}"' + (f" AND ({jql})" if jql else "")
    issues = await jira.search_all(f"{where} ORDER BY Rank ASC, updated DESC", fields=BOARD_FIELDS)
    return {"project": project_key, "columns": columns, "issues": [simplify_issue(i) for i in issues]}


@router.get("/priorities")
async def priorities(jira: JiraClient = Depends(get_jira)):
    return [{"id": p["id"], "name": p["name"]} for p in await jira.priorities()]


@router.get("/users/assignable")
async def assignable_users(
    project_key: str | None = None,
    issue_key: str | None = None,
    query: str = "",
    jira: JiraClient = Depends(get_jira),
):
    if not project_key and not issue_key:
        raise HTTPException(400, "project_key or issue_key is required")
    users = await jira.assignable_users(project_key=project_key, issue_key=issue_key, query=query)
    return [{"account_id": u["accountId"], "display_name": u.get("displayName")} for u in users]


# ---------------------------------------------------------------- issues


@router.post("/issues", status_code=201)
async def create_issue(body: IssueCreate, jira: JiraClient = Depends(get_jira)):
    created = await jira.create_issue(
        project_key=body.project_key,
        summary=body.summary,
        issue_type=body.issue_type,
        description=body.description,
        priority=body.priority,
        labels=body.labels,
        assignee_account_id=body.assignee_account_id,
        extra_fields=body.extra_fields,
    )
    return simplify_issue(await jira.get_issue(created["key"]))


@router.post("/issues/search")
async def search(body: SearchIn, jira: JiraClient = Depends(get_jira)):
    data = await jira.search(body.jql, fields=body.fields, max_results=body.max_results, next_page_token=body.next_page_token)
    return {
        "issues": [simplify_issue(i) for i in data.get("issues", [])],
        "next_page_token": data.get("nextPageToken"),
        "is_last": data.get("isLast", data.get("nextPageToken") is None),
    }


@router.get("/issues/{issue_key}")
async def get_issue(issue_key: str, raw: bool = Query(False, description="Return the unmodified Jira payload"), jira: JiraClient = Depends(get_jira)):
    issue = await jira.get_issue(issue_key)
    return issue if raw else simplify_issue(issue)


@router.patch("/issues/{issue_key}")
async def update_issue(issue_key: str, body: IssueUpdate, jira: JiraClient = Depends(get_jira)):
    await jira.update_issue(
        issue_key,
        summary=body.summary,
        description=body.description,
        priority=body.priority,
        labels=body.labels,
        extra_fields=body.extra_fields,
        notify_users=body.notify_users,
    )
    return simplify_issue(await jira.get_issue(issue_key))


@router.delete("/issues/{issue_key}", status_code=204)
async def delete_issue(issue_key: str, delete_subtasks: bool = False, jira: JiraClient = Depends(get_jira)):
    await jira.delete_issue(issue_key, delete_subtasks=delete_subtasks)
    return Response(status_code=204)


# ---------------------------------------------------------------- workflow


@router.get("/issues/{issue_key}/transitions")
async def transitions(issue_key: str, jira: JiraClient = Depends(get_jira)):
    items = await jira.get_transitions(issue_key)
    return [{"id": t["id"], "name": t["name"], "to_status": t.get("to", {}).get("name")} for t in items]


@router.post("/issues/{issue_key}/status")
async def change_status(issue_key: str, body: StatusChange, jira: JiraClient = Depends(get_jira)):
    if body.transition_id:
        await jira.transition_issue(issue_key, body.transition_id, comment=body.comment)
    elif body.status:
        await jira.change_status(issue_key, body.status, comment=body.comment)
    else:
        raise HTTPException(400, "Provide either 'status' or 'transition_id'")
    return simplify_issue(await jira.get_issue(issue_key))


@router.put("/issues/{issue_key}/assignee")
async def assign(issue_key: str, body: Assign, jira: JiraClient = Depends(get_jira)):
    await jira.assign_issue(issue_key, body.account_id)
    return simplify_issue(await jira.get_issue(issue_key))


# ---------------------------------------------------------------- comments


@router.get("/issues/{issue_key}/comments")
async def list_comments(issue_key: str, jira: JiraClient = Depends(get_jira)):
    return [simplify_comment(c) for c in await jira.list_comments(issue_key)]


@router.post("/issues/{issue_key}/comments", status_code=201)
async def add_comment(issue_key: str, body: CommentIn, jira: JiraClient = Depends(get_jira)):
    return simplify_comment(await jira.add_comment(issue_key, body.body))


@router.put("/issues/{issue_key}/comments/{comment_id}")
async def update_comment(issue_key: str, comment_id: str, body: CommentIn, jira: JiraClient = Depends(get_jira)):
    return simplify_comment(await jira.update_comment(issue_key, comment_id, body.body))


@router.delete("/issues/{issue_key}/comments/{comment_id}", status_code=204)
async def delete_comment(issue_key: str, comment_id: str, jira: JiraClient = Depends(get_jira)):
    await jira.delete_comment(issue_key, comment_id)
    return Response(status_code=204)
