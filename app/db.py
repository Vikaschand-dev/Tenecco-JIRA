from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JiraToken(Base):
    """One Atlassian OAuth grant per platform user. Tokens are stored encrypted."""

    __tablename__ = "jira_tokens"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JiraSite(Base):
    """A Jira Cloud site (cloud id) the user granted access to."""

    __tablename__ = "jira_sites"
    __table_args__ = (UniqueConstraint("user_id", "cloud_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), ForeignKey("jira_tokens.user_id", ondelete="CASCADE"), index=True)
    cloud_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(512))


class WebhookRegistration(Base):
    __tablename__ = "webhook_registrations"

    webhook_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    cloud_id: Mapped[str] = mapped_column(String(64), index=True)
    jql_filter: Mapped[str] = mapped_column(Text)
    events: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncedIssue(Base):
    """Local mirror of Jira issues, kept current by webhooks."""

    __tablename__ = "synced_issues"
    __table_args__ = (UniqueConstraint("cloud_id", "issue_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cloud_id: Mapped[str] = mapped_column(String(64), index=True)
    issue_id: Mapped[str] = mapped_column(String(32))
    issue_key: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(128), default="")
    assignee: Mapped[str | None] = mapped_column(String(255), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(default=False)
    jira_updated: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookEvent(Base):
    """Raw log of received webhook deliveries (useful for debugging and replay)."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cloud_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)


engine = create_async_engine(get_settings().async_database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as session:
        yield session


__all__ = [
    "AsyncSession",
    "Base",
    "JiraSite",
    "JiraToken",
    "SessionLocal",
    "SyncedIssue",
    "WebhookEvent",
    "WebhookRegistration",
    "get_db",
    "init_db",
    "utcnow",
]
