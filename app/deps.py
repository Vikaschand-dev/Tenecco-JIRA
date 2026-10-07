from fastapi import Depends, HTTPException, Query
from sqlalchemy import select

from app.auth import get_current_user_id
from app.db import AsyncSession, JiraSite, get_db
from app.jira_client import JiraClient


async def get_jira(
    cloud_id: str | None = Query(None, description="Jira site cloud id; defaults to the user's first connected site"),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(JiraSite).where(JiraSite.user_id == user_id)
    if cloud_id:
        stmt = stmt.where(JiraSite.cloud_id == cloud_id)
    site = (await db.scalars(stmt.order_by(JiraSite.id))).first()
    if site is None:
        raise HTTPException(401 if not cloud_id else 404, "Jira is not connected" if not cloud_id else "Unknown Jira site")

    async with JiraClient(db, user_id, site.cloud_id) as client:
        yield client
