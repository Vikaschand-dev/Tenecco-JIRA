from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    app_base_url: str = "http://localhost:8000"
    jira_scopes: str = "read:jira-work write:jira-work read:jira-user manage:jira-webhook offline_access"

    session_secret: str = "dev-only-insecure-secret"
    token_encryption_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./jira_integration.db"

    # How often the background job checks for webhooks that need refreshing.
    webhook_refresh_interval_hours: int = 12

    @property
    def redirect_uri(self) -> str:
        return self.app_base_url.rstrip("/") + "/oauth/callback"

    @property
    def webhook_url(self) -> str:
        return self.app_base_url.rstrip("/") + "/webhooks/jira"


@lru_cache
def get_settings() -> Settings:
    return Settings()
