# Tenecco – Jira Cloud Integration: Implementation Guide

This document describes how the Jira integration in `d:\JIRAAPI` is built: the architecture, the main flows, every module, the data model, the API, security choices, setup, and the problems found during the first connection to `accelance-team-123.atlassian.net`.

The setup checklist and full endpoint list are also in [README.md](README.md).

---

## 1. Overview

Tenecco lets each platform user connect their own Jira Cloud account and then work on Jira from inside Tenecco:

| Capability | Status |
|---|---|
| Connect Jira with OAuth 2.0 (3LO), one grant per platform user | Done |
| Create, read, update, delete and search issues (JQL, paginated) | Done |
| Change status by name or transition id (workflow-aware) | Done |
| Assign, unassign, and search assignable users | Done |
| Comments: list, add, edit, delete (plain text ↔ ADF) | Done |
| Kanban board per project, with drag-and-drop status changes | Done |
| Webhook sync from Jira: verified, mirrored locally, streamed live to the UI | Done. Needs a public HTTPS URL. |
| Automatic webhook renewal before Jira's 30-day expiry | Done |
| Dev tools (branches, commits, PRs, builds) in Jira's Development panel | Not implemented. Requires a Forge or Connect app, or Jira admin OAuth credentials. |

**Stack:** Python 3.12, FastAPI, httpx (async), SQLAlchemy 2 (async) with SQLite in development, and a vanilla HTML/JS frontend with no build step.

---

## 2. Architecture

```
 Browser (Tenecco UI)                 Tenecco backend (FastAPI)                     Atlassian
 ───────────────────                  ─────────────────────────                     ─────────
  Board / Issues / Create  ──REST──►  /api/jira/*  ──► JiraClient ──Bearer token──► api.atlassian.com/ex/jira/{cloudId}/rest/api/3
  "Connect Jira"           ──────►    /oauth/connect ─────────────── redirect ────► auth.atlassian.com/authorize
                           ◄──────    /oauth/callback ◄── code ──────────────────── (user approves)
                                         └─ exchange code → tokens (encrypted in DB)
  Live events (SSE)        ◄──────    /api/jira/events/stream ◄─ EventBus ◄─┐
                                      /webhooks/jira ◄── JWT-signed POST ───┴────── Jira webhooks
                                      background job: refresh webhooks every 12 h ─► PUT /webhook/refresh
```

All Jira calls are made **server side** with the user's OAuth token. The browser never sees Atlassian tokens. It holds only a signed session cookie.

---

## 3. Project structure

```
d:\JIRAAPI
├── app/
│   ├── main.py          App setup: TLS trust store, session middleware, error handlers, routers, UI, lifespan job
│   ├── config.py        Settings loaded from .env (pydantic-settings)
│   ├── auth.py          get_current_user_id(): the platform user identity hook
│   ├── oauth.py         OAuth 2.0 (3LO): connect, callback, token refresh/rotation, site discovery
│   ├── crypto.py        Fernet encryption of tokens at rest
│   ├── db.py            SQLAlchemy models and async engine/session
│   ├── jira_client.py   Async Jira REST v3 client (retries, refresh-on-401, all operations)
│   ├── adf.py           Plain text ↔ Atlassian Document Format conversion
│   ├── schemas.py       Request models and response "simplifiers"
│   ├── deps.py          get_jira dependency: resolves the user's site and yields a JiraClient
│   ├── sync.py          handle_jira_event(): applies webhook events (platform hook point)
│   ├── events.py        In-process pub/sub for Server-Sent Events
│   ├── scheduler.py     Background webhook renewal loop
│   ├── routes/
│   │   ├── issues.py    /api/jira/* issue, board, workflow and comment endpoints
│   │   └── webhooks.py  Webhook receiver, subscription management, synced data, SSE stream
│   └── static/          index.html, app.js, styles.css (Tenecco UI)
├── tests/               pytest suite (23 tests)
├── requirements.txt
├── .env.example         Configuration template (.env is git-ignored)
├── README.md            Setup and API reference
└── IMPLEMENTATION.md    This document
```

---

## 4. Key flows

### 4.1 Connecting a Jira account (OAuth 2.0 3LO)

1. The user clicks **Connect Jira**, which calls `GET /oauth/connect`.
   - If the request arrived on a different host from `APP_BASE_URL` (for example `127.0.0.1` instead of `localhost`), the user is redirected to the canonical host first. Cookies are per host, so the flow has to start on the same host Atlassian will return to.
   - A random `state` value is stored in the session. Up to 5 pending states are kept, so double clicks or several tabs don't cancel each other out.
   - The user is redirected to `auth.atlassian.com/authorize` with the scopes, `prompt=consent`, and the callback URL.
2. The user picks the Jira site and approves.
3. Atlassian redirects to `GET /oauth/callback?code=…&state=…`.
   - The state is validated against the session and then removed, so each state works only once.
   - The code is exchanged at `auth.atlassian.com/oauth/token` for an access token (about 1 hour) and a rotating refresh token.
   - Tokens are encrypted and saved in `jira_tokens`.
   - `GET /oauth/token/accessible-resources` lists the approved sites (cloud ids), which are saved in `jira_sites`.
4. Any failure shows a readable page with a **Try again** link instead of raw JSON or a 500 error.

**Scopes requested (classic):** `read:jira-work write:jira-work read:jira-user manage:jira-webhook offline_access`

### 4.2 Calling Jira (JiraClient)

Every request goes to `https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...`. The client handles:

| Situation | Behaviour |
|---|---|
| Token expires within 60 s | Refreshes before the call |
| Jira returns 401 | Forces one refresh and retries once |
| Concurrent refreshes | Serialized per user with an `asyncio.Lock`. Atlassian rotates refresh tokens, so parallel refreshes would invalidate each other. |
| New refresh token returned | Always stored, because the old one is now invalid |
| Refresh token revoked or expired | Raises `NotConnectedError`, which the API returns as `401 {"error":"not_connected"}`. The UI then shows Connect. |
| 429 or 503 rate limiting | Up to 3 retries, honouring `Retry-After` (backoff capped at 30 s) |
| Other 4xx/5xx | Raises `JiraAPIError`, which the API returns as `{"error":"jira_error","jira_status":…,"detail":…}` |

### 4.3 Changing status

Jira statuses can only change through **workflow transitions**. `change_status(issue, "Done")`:

1. Calls `GET /issue/{key}/transitions` to list the transitions available from the current status.
2. Picks the transition whose target status, or failing that whose transition name, matches. The match ignores case.
3. Calls `POST /issue/{key}/transitions`, with an optional comment converted to ADF.
4. If nothing matches, returns 400 with the list of statuses that *are* reachable.

The API also accepts an explicit `transition_id`.

### 4.4 Board

`GET /api/jira/projects/{key}/board` returns:

- **columns:** the distinct statuses from `GET /project/{key}/statuses` across all issue types. They are ordered by status category (To Do → In Progress → Done), with names containing "progress" first among in-progress statuses. Jira's public API doesn't expose column order for business boards. Users can reorder columns in the UI, and the order is saved in their browser.
- **issues:** every issue in the project, fetched through `POST /search/jql` with `nextPageToken` pagination (up to 1,000), simplified for the UI.

The UI moves a card optimistically when it's dropped, calls the status endpoint, and moves the card back with an explanation if the workflow doesn't allow that transition. The board refreshes every 30 seconds, and immediately when a webhook event arrives.

For APLDTT, the columns are: **To Do · In Progress · Testing · HOLD · Move To Production · Done**.

### 4.5 Webhook sync (Jira → Tenecco)

1. **Subscribe:** `POST /api/jira/webhooks {"jql_filter":"project = APLDTT"}` registers a dynamic webhook through `POST /rest/api/3/webhook`. The webhook id, site and expiry are stored in `webhook_registrations`.
   - Default events: `jira:issue_created`, `jira:issue_updated`, `jira:issue_deleted`, `comment_created`, `comment_updated`, `comment_deleted`.
   - Webhook JQL supports only `project`, `issueKey`, `issuetype`, `status`, `priority`, `assignee`, `reporter` with `=`, `!=`, `IN`, `NOT IN`.
2. **Receive:** Jira POSTs to `/webhooks/jira` with `Authorization: Bearer <JWT>`.
   - The JWT is verified with **HS256 and the app's client secret** (30 s leeway). Invalid or missing tokens get 401.
   - The site is resolved from `matchedWebhookIds` through `webhook_registrations`.
   - `handle_jira_event` stores the raw event (`webhook_events`), upserts the issue mirror (`synced_issues`), extracts the changelog and comment details, and publishes a summary to the SSE bus.
3. **Renew:** Jira expires REST-registered webhooks after **30 days**. `scheduler.py` runs every 12 hours and refreshes any registration expiring within 7 days through `PUT /rest/api/3/webhook/refresh`.

**Requirement:** Jira only delivers to **public HTTPS** URLs, on port 443 or a small allow-list of ports and never port 80. Locally this needs a tunnel such as `ngrok http 8000`, with `APP_BASE_URL` set to the tunnel URL.

---

## 5. Data model

| Table | Purpose | Key columns |
|---|---|---|
| `jira_tokens` | One OAuth grant per platform user | `user_id` (PK), `access_token_enc`, `refresh_token_enc`, `expires_at`, `scope` |
| `jira_sites` | Sites the user approved | `user_id`, `cloud_id`, `name`, `url` (unique per user + cloud id) |
| `webhook_registrations` | Webhooks created by Tenecco | `webhook_id` (PK), `user_id`, `cloud_id`, `jql_filter`, `events`, `expires_at` |
| `synced_issues` | Local mirror kept current by webhooks | `cloud_id`, `issue_id`, `issue_key`, `summary`, `status`, `assignee`, `priority`, `deleted` |
| `webhook_events` | Raw delivery log for debugging and replay | `received_at`, `cloud_id`, `event_type`, `issue_key`, `payload` (JSON) |

Tables are created automatically at startup (`init_db`). For production, switch to Postgres and add migrations (e.g. Alembic).

---

## 6. API summary

All `/api/jira/*` endpoints take an optional `?cloud_id=`. The default is the user's first site.

| Area | Endpoints |
|---|---|
| OAuth | `GET /oauth/connect`, `GET /oauth/callback`, `GET /oauth/status`, `POST /oauth/sites/refresh`, `POST /oauth/disconnect` |
| Metadata | `GET /me`, `GET /projects`, `GET /projects/{key}/issue-types`, `GET /priorities`, `GET /users/assignable` |
| Board | `GET /projects/{key}/board?jql=` |
| Issues | `POST /issues`, `POST /issues/search`, `GET /issues/{key}`, `PATCH /issues/{key}`, `DELETE /issues/{key}` |
| Workflow | `GET /issues/{key}/transitions`, `POST /issues/{key}/status`, `PUT /issues/{key}/assignee` |
| Comments | `GET/POST /issues/{key}/comments`, `PUT/DELETE /issues/{key}/comments/{id}` |
| Webhooks | `POST/GET /webhooks`, `DELETE /webhooks/{id}`, `POST /webhooks/refresh`, `GET /synced-issues`, `GET /events`, `GET /events/stream` |
| Jira → us | `POST /webhooks/jira` (no `/api/jira` prefix) |

Interactive documentation: `http://localhost:8000/docs`.

**Example response (simplified issue):**

```json
{
  "id": "10042", "key": "APLDTT-82", "summary": "Checkout fails", "description": "Steps...",
  "status": "To Do", "status_category": "new", "issue_type": "Task", "priority": "High",
  "assignee": { "account_id": "5b10a...", "display_name": "mala dhanraj", "avatar": "https://..." },
  "reporter": { "account_id": "...", "display_name": "vikaschand.godugu", "avatar": "https://..." },
  "labels": [], "parent": null, "project": "APLDTT",
  "created": "2026-10-07T10:00:00.000+0530", "updated": "2026-10-07T10:00:00.000+0530"
}
```

---

## 7. Security

| Concern | Implementation |
|---|---|
| Tokens at rest | Fernet-encrypted with `TOKEN_ENCRYPTION_KEY`. In development without a key, one is derived from `SESSION_SECRET` and a warning is logged. |
| Tokens in the browser | Never sent. The browser holds only a signed session cookie (`SameSite=Lax`, and `Secure` when the base URL is HTTPS). |
| OAuth CSRF | Single-use random `state` stored in the session and compared in constant time |
| Webhook authenticity | HS256 JWT verified with the client secret. Unsigned or invalid requests are rejected. |
| TLS verification | Always on. The app uses the **Windows/OS certificate store** (`truststore`) so it works behind antivirus or proxy HTTPS inspection without turning verification off. |
| Least privilege | Classic scopes only for what's used. A *Resource-level* app, so each grant covers one chosen site. |
| Secrets | Kept in `.env`, which is git-ignored and never committed or shared |

---

## 8. Configuration (`.env`)

| Variable | Required | Example | Notes |
|---|---|---|---|
| `ATLASSIAN_CLIENT_ID` | Yes | | From developer.atlassian.com → Tenecco → Settings |
| `ATLASSIAN_CLIENT_SECRET` | Yes | | Same place. Also used to verify webhooks. |
| `APP_BASE_URL` | Yes | `http://localhost:8000` | Must match the console callback URL: `{APP_BASE_URL}/oauth/callback` |
| `SESSION_SECRET` | Yes | long random string | Signs the session cookie |
| `TOKEN_ENCRYPTION_KEY` | Production | Fernet key | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./jira_integration.db` | Use `postgresql+asyncpg://…` in production |
| `JIRA_SCOPES` | No | (default list) | Must match the scopes added in the developer console |

**Atlassian developer console (app "Tenecco"):**
- Access type: **Resource-level**
- Permissions → Jira API → classic scopes: `read:jira-work`, `write:jira-work`, `read:jira-user`, `manage:jira-webhook`
- Authorization → OAuth 2.0 (3LO) → callback URL: `http://localhost:8000/oauth/callback` (development)

---

## 9. Running locally

```powershell
cd d:\JIRAAPI
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# create .env from .env.example and fill in the values
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app --timeout-graceful-shutdown 2
```

Open **http://localhost:8000**. Use `localhost`, because `127.0.0.1` is redirected automatically. Click **Connect Jira**, choose `accelance-team-123`, then **Accept**.

- `--reload-dir app` restricts reloads to app code, so installing packages doesn't trigger restarts.
- `--timeout-graceful-shutdown 2` stops reloads from hanging on the browser's open live-events (SSE) connection.

**Tests:** `.\.venv\Scripts\pytest`. There are 23 tests covering ADF conversion, token refresh and rotation, 401 retry, rate-limit retry, status transitions, OAuth state handling, the canonical-host redirect, webhook JWT verification and sync, and the issue and board routes.

---

## 10. Issues found during first connection, and fixes

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Atlassian "Something went wrong" page | App scopes or callback not yet configured in the developer console | Added the 4 classic scopes and the exact callback URL |
| 2 | `{"detail":"Invalid OAuth state"}` | The session cookie was set on a different host (`127.0.0.1`) from the one Atlassian returned to (`localhost`) | Redirect to the `APP_BASE_URL` host before starting OAuth and before serving the UI. Keep up to 5 pending states. Show a friendly retry page. |
| 3 | `Internal Server Error` on callback | `CERTIFICATE_VERIFY_FAILED`. **Avast Web/Mail Shield** re-signs HTTPS traffic, and Python's bundled certificate list doesn't trust Avast's root certificate. | `truststore.inject_into_ssl()` so Python uses the Windows certificate store. Verification stays on. |
| 4 | Create-issue dropdowns empty | `GET /priority/search` needs an admin scope (401), and the UI loaded projects and priorities in one `Promise.all`, so one failure blanked everything | Switched to `GET /priority`. The UI now loads each part independently. |
| 5 | Server auto-reload hung | Uvicorn waited forever for the open SSE connection to close | `--timeout-graceful-shutdown 2` and `--reload-dir app` |

---

## 11. Integrating into the Tenecco platform

| Hook | File | What to do |
|---|---|---|
| **User identity** | [app/auth.py](app/auth.py) | Replace `get_current_user_id` so it returns Tenecco's logged-in user id (from your JWT or session). Every Jira connection is keyed by this value. |
| **React to Jira changes** | [app/sync.py](app/sync.py) → `handle_jira_event` | Update Tenecco's own records, send notifications, or run automations. The payload has the issue, the changelog (e.g. `status: To Do → In Progress`), the comment, and the actor. |
| **Call Jira from backend code** | [app/jira_client.py](app/jira_client.py) | `async with JiraClient(db, user_id, cloud_id) as jira: await jira.create_issue(...)` |
| **Custom fields** | `extra_fields` on create and update | e.g. `{"customfield_10016": 5}` |
| **UI** | [app/static/](app/static/) | Use as-is, or call `/api/jira/*` from Tenecco's own frontend |

Inputs still needed from the Tenecco side:
1. How Tenecco authenticates users, and which user id field to use.
2. What a Jira ticket maps to in Tenecco (task, ticket, …) and which fields to sync.
3. Status mapping between Tenecco and the APLDTT workflow.
4. Actions to take on Jira events (e.g. "Done in Jira → mark Tenecco task complete").

---

## 12. Production checklist

- [ ] Deploy behind HTTPS on a real domain. Update `APP_BASE_URL` and add `{APP_BASE_URL}/oauth/callback` in the developer console.
- [ ] Set `TOKEN_ENCRYPTION_KEY` and a strong `SESSION_SECRET`.
- [ ] Switch to Postgres (`asyncpg`) and add migrations.
- [ ] Replace `get_current_user_id` with Tenecco authentication.
- [ ] With more than one instance, move the SSE bus to Redis pub/sub and run the webhook renewal job on a single instance.
- [ ] Subscribe webhooks for the projects you need (e.g. `project = APLDTT`) and confirm live events arrive.
- [ ] To let other organisations connect, set Distribution → **Sharing** in the developer console.
- [ ] Monitor: refresh tokens expire after 90 days of inactivity, and those users must reconnect (`not_connected`).

---

## 13. Possible next steps

- **Dev tools linking:** branches, commits, PRs, builds and deployments in Jira's Development panel. Needs a Forge or Connect app, or Jira admin OAuth credentials.
- Attachments: upload and download.
- Sprint and backlog views for Jira Software projects (Agile API with granular scopes).
- Two-way field mapping between Tenecco entities and Jira issues.
