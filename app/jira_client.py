"""Async Jira Cloud REST API v3 client for OAuth 2.0 (3LO) apps.

Requests go through https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...
The client transparently refreshes expired tokens and retries on 401 (once)
and on 429/503 rate limiting (honouring Retry-After).
"""

import asyncio
from typing import Any

import httpx

from app.adf import text_to_adf
from app.db import AsyncSession
from app.oauth import get_valid_access_token

API_ROOT = "https://api.atlassian.com/ex/jira"
MAX_RATE_LIMIT_RETRIES = 3


class JiraAPIError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Jira API error {status_code}: {detail}")


class JiraClient:
    def __init__(
        self,
        db: AsyncSession,
        user_id: str,
        cloud_id: str,
        http: httpx.AsyncClient | None = None,
    ):
        self.db = db
        self.user_id = user_id
        self.cloud_id = cloud_id
        self._http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    # ------------------------------------------------------------------ transport

    @property
    def base_url(self) -> str:
        return f"{API_ROOT}/{self.cloud_id}/rest/api/3"

    async def request(self, method: str, path: str, **kwargs) -> Any:
        url = self.base_url + path
        token = await get_valid_access_token(self.db, self.user_id)
        refreshed = False
        attempts = 0
        while True:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await self._http.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 401 and not refreshed:
                token = await get_valid_access_token(self.db, self.user_id, force_refresh=True)
                refreshed = True
                continue

            if resp.status_code in (429, 503) and attempts < MAX_RATE_LIMIT_RETRIES:
                attempts += 1
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempts
                await asyncio.sleep(min(delay, 30))
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except ValueError:
                    detail = resp.text
                raise JiraAPIError(resp.status_code, detail)

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    # ------------------------------------------------------------------ metadata

    async def myself(self) -> dict:
        return await self.request("GET", "/myself")

    async def list_projects(self, query: str | None = None, max_results: int = 50) -> list[dict]:
        params: dict[str, Any] = {"maxResults": max_results, "orderBy": "name"}
        if query:
            params["query"] = query
        data = await self.request("GET", "/project/search", params=params)
        return data.get("values", [])

    async def project_issue_types(self, project_key: str) -> list[dict]:
        data = await self.request("GET", f"/issue/createmeta/{project_key}/issuetypes")
        return data.get("issueTypes", data.get("values", []))

    async def priorities(self) -> list[dict]:
        # GET /priority works with read:jira-work; /priority/search needs admin scope.
        return await self.request("GET", "/priority")

    async def project_statuses(self, project_key: str) -> list[dict]:
        """Distinct statuses used by a project's workflows (across all issue types)."""
        data = await self.request("GET", f"/project/{project_key}/statuses")
        seen: dict[str, dict] = {}
        for issue_type in data:
            for s in issue_type.get("statuses", []):
                seen.setdefault(s["id"], s)
        return list(seen.values())

    async def search_all(self, jql: str, fields: list[str] | None = None, limit: int = 1000) -> list[dict]:
        """Follow nextPageToken pagination until `limit` issues are collected."""
        issues: list[dict] = []
        token = None
        while len(issues) < limit:
            page = await self.search(jql, fields=fields, max_results=min(100, limit - len(issues)), next_page_token=token)
            issues.extend(page.get("issues", []))
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                break
        return issues

    async def assignable_users(self, project_key: str | None = None, issue_key: str | None = None, query: str = "") -> list[dict]:
        params: dict[str, Any] = {"query": query, "maxResults": 50}
        if issue_key:
            params["issueKey"] = issue_key
        elif project_key:
            params["project"] = project_key
        return await self.request("GET", "/user/assignable/search", params=params)

    # ------------------------------------------------------------------ issues

    async def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str,
        description: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        assignee_account_id: str | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            # Accept an issue type id ("10001") or name ("Bug").
            "issuetype": {"id": issue_type} if issue_type.isdigit() else {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        if priority:
            fields["priority"] = {"id": priority} if priority.isdigit() else {"name": priority}
        if labels:
            fields["labels"] = labels
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if extra_fields:
            fields.update(extra_fields)
        return await self.request("POST", "/issue", json={"fields": fields})

    async def get_issue(self, issue_key: str, fields: str = "*navigable", expand: str | None = None) -> dict:
        params = {"fields": fields}
        if expand:
            params["expand"] = expand
        return await self.request("GET", f"/issue/{issue_key}", params=params)

    async def update_issue(
        self,
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        extra_fields: dict | None = None,
        notify_users: bool = True,
    ) -> None:
        fields: dict[str, Any] = {}
        if summary is not None:
            fields["summary"] = summary
        if description is not None:
            fields["description"] = text_to_adf(description)
        if priority is not None:
            fields["priority"] = {"id": priority} if priority.isdigit() else {"name": priority}
        if labels is not None:
            fields["labels"] = labels
        if extra_fields:
            fields.update(extra_fields)
        if not fields:
            return
        await self.request(
            "PUT",
            f"/issue/{issue_key}",
            params={"notifyUsers": str(notify_users).lower()},
            json={"fields": fields},
        )

    async def delete_issue(self, issue_key: str, delete_subtasks: bool = False) -> None:
        await self.request("DELETE", f"/issue/{issue_key}", params={"deleteSubtasks": str(delete_subtasks).lower()})

    async def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int = 50,
        next_page_token: str | None = None,
    ) -> dict:
        """Enhanced JQL search (POST /search/jql) with token-based pagination."""
        body: dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or ["summary", "status", "assignee", "priority", "issuetype", "updated", "created"],
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        return await self.request("POST", "/search/jql", json=body)

    # ------------------------------------------------------------------ workflow

    async def get_transitions(self, issue_key: str) -> list[dict]:
        data = await self.request("GET", f"/issue/{issue_key}/transitions")
        return data.get("transitions", [])

    async def transition_issue(
        self,
        issue_key: str,
        transition_id: str,
        comment: str | None = None,
        fields: dict | None = None,
    ) -> None:
        body: dict[str, Any] = {"transition": {"id": transition_id}}
        if fields:
            body["fields"] = fields
        if comment:
            body["update"] = {"comment": [{"add": {"body": text_to_adf(comment)}}]}
        await self.request("POST", f"/issue/{issue_key}/transitions", json=body)

    async def change_status(self, issue_key: str, status_name: str, comment: str | None = None) -> dict:
        """Move an issue to a status by *name*, picking the matching available transition.

        Jira statuses can only be changed through workflow transitions; this finds the
        transition whose target status (or transition name) matches.
        """
        transitions = await self.get_transitions(issue_key)
        wanted = status_name.strip().lower()
        match = next((t for t in transitions if t.get("to", {}).get("name", "").lower() == wanted), None) or next(
            (t for t in transitions if t.get("name", "").lower() == wanted), None
        )
        if match is None:
            available = sorted({t.get("to", {}).get("name", t.get("name")) for t in transitions})
            raise JiraAPIError(
                400,
                {"message": f"No transition to status '{status_name}' from the current status", "available": available},
            )
        await self.transition_issue(issue_key, match["id"], comment=comment)
        return {"transition_id": match["id"], "status": match.get("to", {}).get("name", status_name)}

    async def assign_issue(self, issue_key: str, account_id: str | None) -> None:
        """Assign to an Atlassian accountId; pass None to unassign."""
        await self.request("PUT", f"/issue/{issue_key}/assignee", json={"accountId": account_id})

    # ------------------------------------------------------------------ comments

    async def list_comments(self, issue_key: str, max_results: int = 50) -> list[dict]:
        data = await self.request(
            "GET", f"/issue/{issue_key}/comment", params={"maxResults": max_results, "orderBy": "-created"}
        )
        return data.get("comments", [])

    async def add_comment(self, issue_key: str, body: str) -> dict:
        return await self.request("POST", f"/issue/{issue_key}/comment", json={"body": text_to_adf(body)})

    async def update_comment(self, issue_key: str, comment_id: str, body: str) -> dict:
        return await self.request("PUT", f"/issue/{issue_key}/comment/{comment_id}", json={"body": text_to_adf(body)})

    async def delete_comment(self, issue_key: str, comment_id: str) -> None:
        await self.request("DELETE", f"/issue/{issue_key}/comment/{comment_id}")

    # ------------------------------------------------------------------ webhooks

    async def register_webhooks(self, url: str, webhooks: list[dict]) -> list[dict]:
        """webhooks: [{"jqlFilter": "project = ABC", "events": ["jira:issue_updated", ...]}]"""
        data = await self.request("POST", "/webhook", json={"url": url, "webhooks": webhooks})
        return data.get("webhookRegistrationResult", [])

    async def list_webhooks(self) -> list[dict]:
        data = await self.request("GET", "/webhook", params={"maxResults": 100})
        return data.get("values", [])

    async def delete_webhooks(self, webhook_ids: list[int]) -> None:
        await self.request("DELETE", "/webhook", json={"webhookIds": webhook_ids})

    async def refresh_webhooks(self, webhook_ids: list[int]) -> dict:
        return await self.request("PUT", "/webhook/refresh", json={"webhookIds": webhook_ids})
