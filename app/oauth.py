"""Atlassian OAuth 2.0 (3LO): authorization, token exchange, refresh and storage."""

import asyncio
import html
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select

from app.auth import get_current_user_id
from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.db import AsyncSession, JiraSite, JiraToken, get_db

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Refresh a little before actual expiry to avoid racing the clock.
EXPIRY_SKEW = timedelta(seconds=60)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

# Atlassian rotates refresh tokens: two concurrent refreshes with the same token
# would invalidate one another, so serialize refreshes per user.
_refresh_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class NotConnectedError(Exception):
    pass


def build_authorize_url(state: str) -> str:
    settings = get_settings()
    params = {
        "audience": "api.atlassian.com",
        "client_id": settings.atlassian_client_id,
        "scope": settings.jira_scopes,
        "redirect_uri": settings.redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def _token_request(payload: dict) -> dict:
    settings = get_settings()
    body = {
        "client_id": settings.atlassian_client_id,
        "client_secret": settings.atlassian_client_secret,
        **payload,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(TOKEN_URL, json=body)
    except httpx.HTTPError as exc:
        log.error("Could not reach Atlassian token endpoint: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not reach Atlassian: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Atlassian token endpoint error: {resp.text}")
    return resp.json()


async def _save_tokens(db: AsyncSession, user_id: str, data: dict) -> JiraToken:
    token = await db.get(JiraToken, user_id)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600)))
    if token is None:
        token = JiraToken(user_id=user_id)
        db.add(token)
    token.access_token_enc = encrypt(data["access_token"])
    # Rotating refresh tokens: always store the newest one when provided.
    if data.get("refresh_token"):
        token.refresh_token_enc = encrypt(data["refresh_token"])
    token.expires_at = expires_at
    token.scope = data.get("scope", token.scope or "")
    await db.commit()
    return token


async def _sync_sites(db: AsyncSession, user_id: str, access_token: str) -> list[JiraSite]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
    resp.raise_for_status()
    resources = [r for r in resp.json() if any("jira" in s for s in r.get("scopes", []))]

    await db.execute(delete(JiraSite).where(JiraSite.user_id == user_id))
    sites = [JiraSite(user_id=user_id, cloud_id=r["id"], name=r["name"], url=r["url"]) for r in resources]
    db.add_all(sites)
    await db.commit()
    return sites


async def get_valid_access_token(db: AsyncSession, user_id: str, force_refresh: bool = False) -> str:
    """Return a usable access token, refreshing it when expired (or when forced after a 401)."""
    token = await db.get(JiraToken, user_id)
    if token is None:
        raise NotConnectedError(user_id)

    expires_at = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=timezone.utc)
    if not force_refresh and expires_at - EXPIRY_SKEW > datetime.now(timezone.utc):
        return decrypt(token.access_token_enc)

    async with _refresh_locks[user_id]:
        # Another coroutine may have refreshed while we waited for the lock.
        await db.refresh(token)
        expires_at = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=timezone.utc)
        if expires_at - EXPIRY_SKEW > datetime.now(timezone.utc) and not force_refresh:
            return decrypt(token.access_token_enc)

        try:
            data = await _token_request(
                {"grant_type": "refresh_token", "refresh_token": decrypt(token.refresh_token_enc)}
            )
        except HTTPException as exc:
            # Refresh token revoked/expired (e.g. 90 days of inactivity): user must reconnect.
            raise NotConnectedError(user_id) from exc
        token = await _save_tokens(db, user_id, data)
        return decrypt(token.access_token_enc)


# --------------------------------------------------------------------------- routes


MAX_PENDING_STATES = 5


def is_canonical_host(request: Request) -> bool:
    """True when the request arrived on APP_BASE_URL's host (cookies are per-host)."""
    return request.url.netloc == urlsplit(get_settings().app_base_url).netloc


@router.get("/connect")
async def connect(request: Request):
    settings = get_settings()
    if not settings.atlassian_client_id or not settings.atlassian_client_secret:
        raise HTTPException(500, "ATLASSIAN_CLIENT_ID / ATLASSIAN_CLIENT_SECRET are not configured")
    # Atlassian redirects back to APP_BASE_URL; the session cookie must be set on that
    # same host (e.g. "localhost" vs "127.0.0.1" are different cookie jars).
    if not is_canonical_host(request):
        return RedirectResponse(settings.app_base_url.rstrip("/") + "/oauth/connect")
    get_current_user_id(request)
    state = secrets.token_urlsafe(24)
    # Keep a few pending states so double clicks / multiple tabs don't invalidate each other.
    pending = request.session.get("oauth_states", [])[-(MAX_PENDING_STATES - 1):]
    request.session["oauth_states"] = pending + [state]
    return RedirectResponse(build_authorize_url(state))


def _retry_page(message: str, status_code: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Jira connection failed</title>
<body style="font-family:system-ui;max-width:560px;margin:80px auto;padding:0 16px">
<h2>Couldn't connect Jira</h2><p>{html.escape(message)}</p>
<p><a href="/oauth/connect">Try again</a> &middot; <a href="/">Back to app</a></p></body>""",
        status_code=status_code,
    )


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return _retry_page(f"Atlassian authorization failed: {error}")

    pending = request.session.get("oauth_states", [])
    if not code or not state or not any(secrets.compare_digest(state, s) for s in pending):
        log.warning(
            "OAuth state mismatch: host=%s session_cookie=%s pending_states=%d",
            request.url.netloc,
            "session" in request.cookies,
            len(pending),
        )
        return _retry_page(
            "The sign-in session expired or was started from a different address. "
            "Please start again from this page."
        )
    request.session["oauth_states"] = [s for s in pending if s != state]
    user_id = get_current_user_id(request)

    settings = get_settings()
    try:
        data = await _token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": settings.redirect_uri}
        )
        await _save_tokens(db, user_id, data)
        await _sync_sites(db, user_id, data["access_token"])
    except HTTPException as exc:
        return _retry_page(str(exc.detail), status_code=502)
    except httpx.HTTPError as exc:
        log.exception("Failed to load Jira sites after authorization")
        return _retry_page(f"Connected, but loading your Jira sites failed: {exc}", status_code=502)
    return RedirectResponse("/")


@router.get("/status")
async def status(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    token = await db.get(JiraToken, user_id)
    sites = (await db.scalars(select(JiraSite).where(JiraSite.user_id == user_id))).all()
    return {
        "connected": token is not None,
        "scope": token.scope if token else None,
        "sites": [{"cloud_id": s.cloud_id, "name": s.name, "url": s.url} for s in sites],
    }


@router.post("/sites/refresh")
async def refresh_sites(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    try:
        access_token = await get_valid_access_token(db, user_id)
    except NotConnectedError:
        raise HTTPException(401, "Jira is not connected")
    sites = await _sync_sites(db, user_id, access_token)
    return [{"cloud_id": s.cloud_id, "name": s.name, "url": s.url} for s in sites]


@router.post("/disconnect")
async def disconnect(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(JiraSite).where(JiraSite.user_id == user_id))
    await db.execute(delete(JiraToken).where(JiraToken.user_id == user_id))
    await db.commit()
    return {"connected": False}
