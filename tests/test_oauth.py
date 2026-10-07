from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import oauth
from app.db import JiraSite, JiraToken, SessionLocal
from app.main import app

BASE = "https://example.test"  # APP_BASE_URL in conftest


def _client():
    return TestClient(app, base_url=BASE)


def test_connect_from_other_host_redirects_to_canonical_host():
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.get("/oauth/connect", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == f"{BASE}/oauth/connect"
        assert c.get("/", follow_redirects=False).headers["location"] == f"{BASE}/"


def test_full_oauth_flow(monkeypatch):
    async def fake_token_request(payload):
        assert payload["grant_type"] == "authorization_code"
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "read:jira-work"}

    async def fake_sync_sites(db, user_id, access_token):
        db.add(JiraSite(user_id=user_id, cloud_id="c1", name="accelance", url="https://x.atlassian.net"))
        await db.commit()

    monkeypatch.setattr(oauth, "_token_request", fake_token_request)
    monkeypatch.setattr(oauth, "_sync_sites", fake_sync_sites)

    with _client() as c:
        # Two clicks on "Connect" — the first state must still be accepted.
        first = parse_qs(urlsplit(c.get("/oauth/connect", follow_redirects=False).headers["location"]).query)["state"][0]
        c.get("/oauth/connect", follow_redirects=False)

        r = c.get(f"/oauth/callback?code=abc&state={first}", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/"
        status = c.get("/oauth/status").json()
        assert status["connected"] is True and status["sites"][0]["name"] == "accelance"

        # State is single-use.
        assert c.get(f"/oauth/callback?code=abc&state={first}").status_code == 400


async def test_callback_without_matching_state_shows_retry_page():
    with _client() as c:
        r = c.get("/oauth/callback?code=abc&state=forged")
    assert r.status_code == 400 and "Try again" in r.text
    async with SessionLocal() as db:
        assert (await db.scalars(select(JiraToken))).first() is None
