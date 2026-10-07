import os
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    app_base_url: str = "http://localhost:8000"
    jira_scopes: str = "read:jira-work write:jira-work read:jira-user manage:jira-webhook offline_access"

    session_secret: str = "dev-only-insecure-secret"
    token_encryption_key: str = ""

    database_url: str = ""

    # Shared secret Vercel Cron sends as `Authorization: Bearer <CRON_SECRET>`.
    cron_secret: str = ""

    # How often the background job checks for webhooks that need refreshing.
    webhook_refresh_interval_hours: int = 12

    @property
    def serverless(self) -> bool:
        # Vercel sets VERCEL=1; there the filesystem is read-only (except /tmp) and
        # background tasks don't survive between requests.
        return bool(os.environ.get("VERCEL"))

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if not url:
            # /tmp is the only writable path on Vercel, and it is wiped on cold starts.
            path = "/tmp/jira_integration.db" if self.serverless else "./jira_integration.db"
            return f"sqlite+aiosqlite:///{path}"
        # Accept plain postgres URLs as given by Vercel/Neon and switch them to asyncpg.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix):]
                break
        if url.startswith("postgresql+asyncpg://"):
            # asyncpg doesn't understand libpq's sslmode/channel_binding query params.
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            sslmode = query.pop("sslmode", None)
            query.pop("channel_binding", None)
            if sslmode and sslmode != "disable":
                query["ssl"] = "require"
            url = urlunsplit(parts._replace(query=urlencode(query)))
        return url

    @property
    def redirect_uri(self) -> str:
        return self.app_base_url.rstrip("/") + "/oauth/callback"

    @property
    def webhook_url(self) -> str:
        return self.app_base_url.rstrip("/") + "/webhooks/jira"


@lru_cache
def get_settings() -> Settings:
    return Settings()
