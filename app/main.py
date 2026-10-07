import truststore

# Verify TLS against the OS certificate store instead of Python's bundled list, so the
# app works behind corporate proxies / antivirus HTTPS scanning (e.g. Avast Web Shield).
truststore.inject_into_ssl()

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import logging  # noqa: E402
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import oauth
from app.config import get_settings
from app.db import init_db
from app.jira_client import JiraAPIError
from app.oauth import NotConnectedError
from app.routes import issues, webhooks
from app.scheduler import webhook_refresh_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    task = asyncio.create_task(webhook_refresh_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


settings = get_settings()
app = FastAPI(title="Jira Integration", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.app_base_url.startswith("https://"),
)


@app.exception_handler(JiraAPIError)
async def jira_error_handler(_: Request, exc: JiraAPIError):
    status = exc.status_code if 400 <= exc.status_code < 600 else 502
    # Jira's 401 here means the grant lacks access (we already retried with a fresh token).
    if status == 401:
        status = 403
    return JSONResponse(status_code=status, content={"error": "jira_error", "jira_status": exc.status_code, "detail": exc.detail})


@app.exception_handler(NotConnectedError)
async def not_connected_handler(_: Request, __: NotConnectedError):
    return JSONResponse(status_code=401, content={"error": "not_connected", "detail": "Connect your Jira account first"})


app.include_router(oauth.router)
app.include_router(issues.router)
app.include_router(webhooks.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index(request: Request):
    # Serve the UI only on APP_BASE_URL's host so the session cookie matches the OAuth callback.
    if not oauth.is_canonical_host(request):
        return RedirectResponse(settings.app_base_url.rstrip("/") + "/")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}
