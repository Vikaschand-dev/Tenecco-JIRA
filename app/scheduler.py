"""Background job: Jira expires REST-registered webhooks after 30 days, so keep refreshing them."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.config import get_settings
from app.db import SessionLocal, WebhookRegistration
from app.jira_client import JiraClient
from app.oauth import NotConnectedError
from app.sync import default_webhook_expiry, parse_jira_datetime

log = logging.getLogger(__name__)

REFRESH_WHEN_WITHIN = timedelta(days=7)


async def refresh_expiring_webhooks() -> int:
    threshold = datetime.now(timezone.utc) + REFRESH_WHEN_WITHIN
    refreshed = 0
    async with SessionLocal() as db:
        regs = (
            await db.scalars(
                select(WebhookRegistration).where(
                    or_(WebhookRegistration.expires_at.is_(None), WebhookRegistration.expires_at < threshold)
                )
            )
        ).all()

        groups: dict[tuple[str, str], list[WebhookRegistration]] = defaultdict(list)
        for r in regs:
            groups[(r.user_id, r.cloud_id)].append(r)

        for (user_id, cloud_id), items in groups.items():
            try:
                async with JiraClient(db, user_id, cloud_id) as jira:
                    result = await jira.refresh_webhooks([r.webhook_id for r in items])
                expiry = parse_jira_datetime((result or {}).get("expirationDate")) or default_webhook_expiry()
                for r in items:
                    r.expires_at = expiry
                await db.commit()
                refreshed += len(items)
            except NotConnectedError:
                log.warning("Cannot refresh webhooks for user %s: Jira disconnected", user_id)
            except Exception:
                log.exception("Failed refreshing webhooks for user %s site %s", user_id, cloud_id)
    return refreshed


async def webhook_refresh_loop() -> None:
    interval = get_settings().webhook_refresh_interval_hours * 3600
    while True:
        try:
            n = await refresh_expiring_webhooks()
            if n:
                log.info("Refreshed %d Jira webhooks", n)
        except Exception:
            log.exception("Webhook refresh loop error")
        await asyncio.sleep(interval)
