from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import jira_client, oauth
from app.crypto import encrypt
from app.db import JiraToken, SessionLocal
from app.jira_client import JiraAPIError, JiraClient

USER = "user-1"
CLOUD = "cloud-123"


async def _store_token(db, access="old-access", expires_in=3600):
    db.add(
        JiraToken(
            user_id=USER,
            access_token_enc=encrypt(access),
            refresh_token_enc=encrypt("refresh-1"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scope="read:jira-work",
        )
    )
    await db.commit()


@pytest.fixture
def token_endpoint(monkeypatch):
    calls = []

    async def fake_token_request(payload):
        calls.append(payload)
        n = len(calls)
        return {"access_token": f"new-access-{n}", "refresh_token": f"refresh-{n + 1}", "expires_in": 3600, "scope": "x"}

    monkeypatch.setattr(oauth, "_token_request", fake_token_request)
    return calls


def _client(db, handler):
    return JiraClient(db, USER, CLOUD, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_uses_cloud_gateway_url_and_bearer_token():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"accountId": "abc"})

    async with SessionLocal() as db:
        await _store_token(db)
        assert (await _client(db, handler).myself())["accountId"] == "abc"

    assert seen["url"] == f"https://api.atlassian.com/ex/jira/{CLOUD}/rest/api/3/myself"
    assert seen["auth"] == "Bearer old-access"


async def test_expired_token_is_refreshed_and_rotated_refresh_token_saved(token_endpoint):
    def handler(request):
        return httpx.Response(200, json={"ok": request.headers["Authorization"]})

    async with SessionLocal() as db:
        await _store_token(db, expires_in=-10)
        result = await _client(db, handler).request("GET", "/myself")
        assert result == {"ok": "Bearer new-access-1"}
        assert token_endpoint[0] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}

        from app.crypto import decrypt

        tok = await db.get(JiraToken, USER)
        assert decrypt(tok.refresh_token_enc) == "refresh-2"


async def test_401_triggers_single_forced_refresh_and_retry(token_endpoint):
    auths = []

    def handler(request):
        auths.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old-access":
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"done": True})

    async with SessionLocal() as db:
        await _store_token(db)
        assert await _client(db, handler).request("GET", "/myself") == {"done": True}
    assert auths == ["Bearer old-access", "Bearer new-access-1"]
    assert len(token_endpoint) == 1


async def test_rate_limit_retries_with_retry_after(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(jira_client.asyncio, "sleep", fake_sleep)
    responses = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": 1})])

    async with SessionLocal() as db:
        await _store_token(db)
        assert await _client(db, lambda r: next(responses)).request("GET", "/x") == {"ok": 1}
    assert sleeps == [2.0]


async def test_errors_raise_with_jira_detail():
    def handler(request):
        return httpx.Response(400, json={"errorMessages": [], "errors": {"summary": "required"}})

    async with SessionLocal() as db:
        await _store_token(db)
        with pytest.raises(JiraAPIError) as exc:
            await _client(db, handler).create_issue("ABC", "", "Bug")
    assert exc.value.status_code == 400
    assert exc.value.detail["errors"]["summary"] == "required"


async def test_create_issue_payload():
    captured = {}

    def handler(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": "1", "key": "ABC-1"})

    async with SessionLocal() as db:
        await _store_token(db)
        await _client(db, handler).create_issue(
            "ABC", "Login broken", "Bug", description="Steps", priority="High", labels=["web"], assignee_account_id="acc"
        )
    f = captured["fields"]
    assert f["project"] == {"key": "ABC"}
    assert f["issuetype"] == {"name": "Bug"}
    assert f["priority"] == {"name": "High"}
    assert f["assignee"] == {"accountId": "acc"}
    assert f["description"]["type"] == "doc"


TRANSITIONS = {
    "transitions": [
        {"id": "11", "name": "Start work", "to": {"name": "In Progress"}},
        {"id": "31", "name": "Resolve", "to": {"name": "Done"}},
    ]
}


async def test_change_status_picks_transition_by_target_status():
    posted = {}

    def handler(request):
        import json

        if request.method == "GET":
            return httpx.Response(200, json=TRANSITIONS)
        posted.update(json.loads(request.content))
        return httpx.Response(204)

    async with SessionLocal() as db:
        await _store_token(db)
        result = await _client(db, handler).change_status("ABC-1", "done", comment="Shipped")
    assert result == {"transition_id": "31", "status": "Done"}
    assert posted["transition"] == {"id": "31"}
    assert posted["update"]["comment"][0]["add"]["body"]["type"] == "doc"


async def test_change_status_unknown_lists_available():
    async with SessionLocal() as db:
        await _store_token(db)
        with pytest.raises(JiraAPIError) as exc:
            await _client(db, lambda r: httpx.Response(200, json=TRANSITIONS)).change_status("ABC-1", "Blocked")
    assert exc.value.detail["available"] == ["Done", "In Progress"]


async def test_not_connected():
    async with SessionLocal() as db:
        with pytest.raises(oauth.NotConnectedError):
            await _client(db, lambda r: httpx.Response(200)).myself()
