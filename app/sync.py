"""Applies incoming Jira webhook events to the platform.

`handle_jira_event` is the single place to plug in your platform logic
(update your own ticket records, notify users, trigger automations, ...).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.adf import adf_to_text
from app.db import AsyncSession, SyncedIssue, WebhookEvent, WebhookRegistration
from app.events import bus

log = logging.getLogger(__name__)


def parse_jira_datetime(value) -> datetime | None:
    """Jira returns either epoch milliseconds or ISO strings like 2024-06-01T12:42:30.000+0000."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def default_webhook_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


async def resolve_cloud_id(db: AsyncSession, payload: dict) -> str | None:
    ids = payload.get("matchedWebhookIds") or []
    if not ids:
        return None
    reg = (await db.scalars(select(WebhookRegistration).where(WebhookRegistration.webhook_id.in_(ids)))).first()
    return reg.cloud_id if reg else None


async def _upsert_issue(db: AsyncSession, cloud_id: str, issue: dict, deleted: bool = False) -> SyncedIssue:
    fields = issue.get("fields") or {}
    row = (
        await db.scalars(
            select(SyncedIssue).where(SyncedIssue.cloud_id == cloud_id, SyncedIssue.issue_id == str(issue["id"]))
        )
    ).first()
    if row is None:
        row = SyncedIssue(cloud_id=cloud_id, issue_id=str(issue["id"]))
        db.add(row)
    row.issue_key = issue.get("key", row.issue_key)
    if fields:
        row.summary = fields.get("summary", row.summary) or ""
        row.status = (fields.get("status") or {}).get("name", row.status) or ""
        row.assignee = (fields.get("assignee") or {}).get("displayName")
        row.priority = (fields.get("priority") or {}).get("name")
        row.jira_updated = fields.get("updated", row.jira_updated)
    row.deleted = deleted
    return row


async def handle_jira_event(db: AsyncSession, payload: dict) -> dict:
    event_type = payload.get("webhookEvent", "unknown")
    cloud_id = await resolve_cloud_id(db, payload)
    issue = payload.get("issue") or {}
    issue_key = issue.get("key")

    db.add(WebhookEvent(cloud_id=cloud_id, event_type=event_type, issue_key=issue_key, payload=payload))

    summary: dict = {"event": event_type, "issue_key": issue_key, "cloud_id": cloud_id}

    if cloud_id and issue.get("id"):
        row = await _upsert_issue(db, cloud_id, issue, deleted=event_type == "jira:issue_deleted")
        summary.update({"summary": row.summary, "status": row.status, "assignee": row.assignee})

    # What changed (e.g. status From -> To) for issue_updated events.
    changes = [
        {"field": i.get("field"), "from": i.get("fromString"), "to": i.get("toString")}
        for i in (payload.get("changelog") or {}).get("items", [])
    ]
    if changes:
        summary["changes"] = changes

    if event_type.startswith("comment_") and payload.get("comment"):
        c = payload["comment"]
        summary["comment"] = {
            "id": c.get("id"),
            "author": (c.get("author") or {}).get("displayName"),
            "body": adf_to_text(c.get("body")),
        }

    if payload.get("user"):
        summary["actor"] = payload["user"].get("displayName")

    await db.commit()

    # ---- Plug your platform logic in here --------------------------------
    # e.g. update your own ticket table, send notifications, run automations.
    log.info("Jira event %s on %s (%s)", event_type, issue_key, cloud_id)

    if cloud_id:
        bus.publish(cloud_id, summary)
    return summary
