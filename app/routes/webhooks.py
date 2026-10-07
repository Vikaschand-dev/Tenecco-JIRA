import asyncio
import logging

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from app.auth import get_current_user_id
from app.config import get_settings
from app.db import AsyncSession, JiraSite, SyncedIssue, WebhookEvent, WebhookRegistration, get_db
from app.deps import get_jira
from app.events import bus
from app.scheduler import refresh_expiring_webhooks
from app.jira_client import JiraClient
from app.schemas import WebhookSubscribe
from app.sync import default_webhook_expiry, handle_jira_event, parse_jira_datetime

log = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------- cron (called by Vercel Cron)


@router.get("/cron/refresh-webhooks", include_in_schema=False)
async def cron_refresh_webhooks(request: Request):
    secret = get_settings().cron_secret
    if not secret or request.headers.get("Authorization") != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")
    return {"refreshed": await refresh_expiring_webhooks()}


# ---------------------------------------------------------------- receiver (called by Jira)


def verify_webhook_jwt(request: Request) -> dict:
    """Webhooks for OAuth 2.0 apps carry `Authorization: Bearer <JWT>` signed (HS256) with the client secret."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    try:
        return jwt.decode(
            token,
            get_settings().atlassian_client_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        log.warning("Rejected webhook with invalid JWT: %s", exc)
        raise HTTPException(401, "Invalid webhook signature")


@router.post("/webhooks/jira", include_in_schema=False)
async def receive_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    verify_webhook_jwt(request)
    payload = await request.json()
    await handle_jira_event(db, payload)
    return {"ok": True}


# ---------------------------------------------------------------- management (called by the platform)


@router.post("/api/jira/webhooks", status_code=201)
async def subscribe(
    body: WebhookSubscribe,
    user_id: str = Depends(get_current_user_id),
    jira: JiraClient = Depends(get_jira),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    if not settings.webhook_url.startswith("https://"):
        raise HTTPException(400, "APP_BASE_URL must be a public HTTPS URL for Jira to deliver webhooks")

    results = await jira.register_webhooks(settings.webhook_url, [{"jqlFilter": body.jql_filter, "events": body.events}])
    created, errors = [], []
    for r in results:
        if "createdWebhookId" in r:
            wid = int(r["createdWebhookId"])
            await db.merge(
                WebhookRegistration(
                    webhook_id=wid,
                    user_id=user_id,
                    cloud_id=jira.cloud_id,
                    jql_filter=body.jql_filter,
                    events=body.events,
                    expires_at=default_webhook_expiry(),
                )
            )
            created.append(wid)
        else:
            errors.extend(r.get("errors", [r]))
    await db.commit()

    # Pick up the exact expiry dates Jira assigned.
    if created:
        await _sync_expiry_from_remote(jira, db)
    if errors and not created:
        raise HTTPException(400, {"errors": errors})
    return {"created_webhook_ids": created, "errors": errors}


async def _sync_expiry_from_remote(jira: JiraClient, db: AsyncSession) -> None:
    for w in await jira.list_webhooks():
        reg = await db.get(WebhookRegistration, int(w["id"]))
        if reg and (exp := parse_jira_datetime(w.get("expirationDate"))):
            reg.expires_at = exp
    await db.commit()


@router.get("/api/jira/webhooks")
async def list_subscriptions(jira: JiraClient = Depends(get_jira)):
    return [
        {
            "id": w["id"],
            "jql_filter": w.get("jqlFilter"),
            "events": w.get("events", []),
            "expires_at": parse_jira_datetime(w.get("expirationDate")),
        }
        for w in await jira.list_webhooks()
    ]


@router.delete("/api/jira/webhooks/{webhook_id}", status_code=204)
async def unsubscribe(webhook_id: int, jira: JiraClient = Depends(get_jira), db: AsyncSession = Depends(get_db)):
    await jira.delete_webhooks([webhook_id])
    await db.execute(delete(WebhookRegistration).where(WebhookRegistration.webhook_id == webhook_id))
    await db.commit()


@router.post("/api/jira/webhooks/refresh")
async def refresh_now(jira: JiraClient = Depends(get_jira), db: AsyncSession = Depends(get_db)):
    ids = [int(w["id"]) for w in await jira.list_webhooks()]
    if not ids:
        return {"refreshed": []}
    await jira.refresh_webhooks(ids)
    await _sync_expiry_from_remote(jira, db)
    return {"refreshed": ids}


# ---------------------------------------------------------------- synced data + live stream


async def _user_site(db: AsyncSession, user_id: str, cloud_id: str | None) -> JiraSite:
    stmt = select(JiraSite).where(JiraSite.user_id == user_id)
    if cloud_id:
        stmt = stmt.where(JiraSite.cloud_id == cloud_id)
    site = (await db.scalars(stmt.order_by(JiraSite.id))).first()
    if site is None:
        raise HTTPException(401, "Jira is not connected")
    return site


@router.get("/api/jira/synced-issues")
async def synced_issues(
    cloud_id: str | None = None, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)
):
    site = await _user_site(db, user_id, cloud_id)
    rows = (
        await db.scalars(
            select(SyncedIssue).where(SyncedIssue.cloud_id == site.cloud_id).order_by(SyncedIssue.synced_at.desc()).limit(200)
        )
    ).all()
    return [
        {
            "key": r.issue_key,
            "summary": r.summary,
            "status": r.status,
            "assignee": r.assignee,
            "priority": r.priority,
            "deleted": r.deleted,
            "synced_at": r.synced_at,
        }
        for r in rows
    ]


@router.get("/api/jira/events")
async def recent_events(
    cloud_id: str | None = None,
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    site = await _user_site(db, user_id, cloud_id)
    rows = (
        await db.scalars(
            select(WebhookEvent)
            .where(WebhookEvent.cloud_id == site.cloud_id)
            .order_by(WebhookEvent.received_at.desc())
            .limit(min(limit, 200))
        )
    ).all()
    return [{"id": r.id, "event": r.event_type, "issue_key": r.issue_key, "received_at": r.received_at} for r in rows]


@router.get("/api/jira/events/stream")
async def stream(
    request: Request,
    cloud_id: str | None = None,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    site = await _user_site(db, user_id, cloud_id)
    queue = bus.subscribe(site.cloud_id)

    async def gen():
        try:
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(site.cloud_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
