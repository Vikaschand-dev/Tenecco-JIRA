import time

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal, SyncedIssue, WebhookEvent, WebhookRegistration
from app.main import app

CLOUD = "cloud-123"


def _auth(secret="test-secret"):
    token = jwt.encode({"iss": "jira", "iat": int(time.time()), "exp": int(time.time()) + 60}, secret, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


ISSUE_UPDATED = {
    "webhookEvent": "jira:issue_updated",
    "matchedWebhookIds": [42],
    "user": {"displayName": "Ana"},
    "issue": {
        "id": "10001",
        "key": "ABC-1",
        "fields": {
            "summary": "Login broken",
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Ana"},
            "priority": {"name": "High"},
            "updated": "2026-10-07T10:00:00.000+0000",
        },
    },
    "changelog": {"items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]},
}


@pytest.fixture
async def registration():
    async with SessionLocal() as db:
        db.add(WebhookRegistration(webhook_id=42, user_id="u", cloud_id=CLOUD, jql_filter="project = ABC", events=[]))
        await db.commit()


def test_rejects_missing_or_bad_signature():
    with TestClient(app) as client:
        assert client.post("/webhooks/jira", json=ISSUE_UPDATED).status_code == 401
        assert client.post("/webhooks/jira", json=ISSUE_UPDATED, headers=_auth("wrong")).status_code == 401


async def test_valid_webhook_syncs_issue_and_logs_event(registration):
    with TestClient(app) as client:
        resp = client.post("/webhooks/jira", json=ISSUE_UPDATED, headers=_auth())
        assert resp.status_code == 200

        deleted = {**ISSUE_UPDATED, "webhookEvent": "jira:issue_deleted", "changelog": None}
        assert client.post("/webhooks/jira", json=deleted, headers=_auth()).status_code == 200

    async with SessionLocal() as db:
        issue = (await db.scalars(select(SyncedIssue))).one()
        assert (issue.issue_key, issue.status, issue.assignee, issue.cloud_id) == ("ABC-1", "In Progress", "Ana", CLOUD)
        assert issue.deleted is True
        events = (await db.scalars(select(WebhookEvent).order_by(WebhookEvent.id))).all()
        assert [e.event_type for e in events] == ["jira:issue_updated", "jira:issue_deleted"]
