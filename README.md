# Jira Cloud Integration (FastAPI)

Lets your platform's users connect their own Jira Cloud account (OAuth 2.0 3LO). From then on they can work on Jira from your platform, and changes made in Jira come back through webhooks.

| Capability | How |
|---|---|
| Connect Jira accounts | OAuth 2.0 (3LO). Tokens are stored encrypted, auto-refreshed, and refresh tokens are rotated. One user can access several Jira sites. |
| Tickets | Create, read, update, delete, and search with JQL (paginated) |
| Status | Change status by name (`"Done"`) or by transition id. Available transitions can be listed. |
| People | Assign or unassign, and search assignable users |
| Comments | List, add, edit, delete. Plain text is converted to and from Atlassian Document Format (ADF). |
| Sync from Jira | Webhooks verified by JWT signature, a local mirror of issues, an event log, a live browser stream (SSE), and automatic refresh before the 30-day expiry |
| Board | A Kanban board per project with one column per workflow status. Dragging a card changes its status in Jira, and clicking a card opens it for editing. You can filter by assignee or text, hide sub-tasks, reorder columns, and the board refreshes automatically every 30 seconds. |
| Web UI | `/` lets you try every feature end to end. Interactive API docs are at `/docs`. |

## 1. Create the Atlassian app

1. Go to <https://developer.atlassian.com/console/myapps/>, then **Create** > **OAuth 2.0 integration**.
2. **Permissions** > **Jira API** > **Configure**. Add these classic scopes:
   `read:jira-work`, `write:jira-work`, `read:jira-user`, `manage:jira-webhook`
   (`offline_access` is requested automatically so tokens can be refreshed).
3. **Authorization** > **OAuth 2.0 (3LO)** > set the callback URL to `https://<your-host>/oauth/callback`.
4. **Settings**: copy the **Client ID** and **Secret**.
5. To let users outside your own Atlassian org connect, go to **Distribution** and set it to *Sharing*.

## 2. Run it

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env    # then fill in the values

# Jira can only deliver webhooks to a public HTTPS URL. In development:
ngrok http 8000           # put the https URL in APP_BASE_URL (and in the app's callback URL)

.\.venv\Scripts\uvicorn app.main:app --reload --reload-dir app --timeout-graceful-shutdown 2 --port 8000
```

Open your `APP_BASE_URL` (not `localhost`, because the session cookie and OAuth callback must use the same host). Click **Connect Jira**, then try creating, editing and transitioning issues. To see sync working, go to **Webhook sync**, subscribe with e.g. `project = ABC`, then change an issue inside Jira. It appears live.

Run the tests with `.\.venv\Scripts\pytest`.

## 3. API reference

All endpoints take an optional `?cloud_id=` to choose among the user's Jira sites. Without it, the first site is used.

| Method | Path | Purpose |
|---|---|---|
| GET | `/oauth/connect` | Start the OAuth flow |
| GET | `/oauth/status` | Connected? Which sites? |
| POST | `/oauth/disconnect` | Remove stored tokens |
| GET | `/api/jira/projects` | Projects |
| GET | `/api/jira/projects/{key}/issue-types` | Issue types for a project |
| GET | `/api/jira/projects/{key}/board` | Board data: status columns and all of the project's issues (optional `?jql=` filter) |
| GET | `/api/jira/priorities` | Priorities |
| GET | `/api/jira/users/assignable?project_key=` or `?issue_key=` | Assignable users |
| POST | `/api/jira/issues` | Create an issue |
| POST | `/api/jira/issues/search` | JQL search (`next_page_token` pagination) |
| GET | `/api/jira/issues/{key}` | Get an issue (`?raw=true` returns the full Jira payload) |
| PATCH | `/api/jira/issues/{key}` | Update summary, description, priority, labels, or any raw/custom fields |
| DELETE | `/api/jira/issues/{key}` | Delete an issue |
| GET | `/api/jira/issues/{key}/transitions` | Available status transitions |
| POST | `/api/jira/issues/{key}/status` | `{"status": "Done"}` or `{"transition_id": "31"}`, with an optional `comment` |
| PUT | `/api/jira/issues/{key}/assignee` | `{"account_id": "..."}`, or `null` to unassign |
| GET/POST | `/api/jira/issues/{key}/comments` | List or add comments |
| PUT/DELETE | `/api/jira/issues/{key}/comments/{id}` | Edit or delete a comment |
| POST/GET | `/api/jira/webhooks` | Subscribe (`{"jql_filter": "project = ABC"}`) or list subscriptions |
| DELETE | `/api/jira/webhooks/{id}` | Unsubscribe |
| POST | `/api/jira/webhooks/refresh` | Extend expiry now (also runs automatically) |
| GET | `/api/jira/synced-issues`, `/api/jira/events` | Local mirror and event log |
| GET | `/api/jira/events/stream` | Server-Sent Events of live Jira changes |
| POST | `/webhooks/jira` | **Called by Jira.** Verifies the JWT, then syncs. |

Example:

```bash
curl -X POST "$BASE/api/jira/issues" -H "Content-Type: application/json" -b cookies.txt \
  -d '{"project_key":"ABC","issue_type":"Bug","summary":"Checkout fails","description":"Steps...","priority":"High"}'

curl -X POST "$BASE/api/jira/issues/ABC-12/status" -H "Content-Type: application/json" -b cookies.txt \
  -d '{"status":"In Progress","comment":"Picked up"}'
```

Errors from Jira are passed through as `{"error":"jira_error","jira_status":400,"detail":{...}}`. If the user hasn't connected Jira, or their grant was revoked, the response is `401 {"error":"not_connected"}`, which tells your UI to show "Connect Jira".

## 4. Plugging it into your platform

- **User identity.** [app/auth.py](app/auth.py) is the only place to change. Make `get_current_user_id` return *your* logged-in user's id, for example from your JWT or session. All Jira connections are stored against that id.
- **Reacting to Jira changes.** [app/sync.py](app/sync.py) `handle_jira_event` runs for every webhook. Add your logic there, such as updating your own ticket table, notifying users, or triggering automations. The payload includes the issue, the changelog (e.g. status `To Do → In Progress`), and comments.
- **Calling Jira from your own backend code.**
  ```python
  async with JiraClient(db, user_id, cloud_id) as jira:
      issue = await jira.create_issue("ABC", "Title", "Task", description="...")
      await jira.change_status(issue["key"], "Done")
  ```
- **Custom fields.** Pass them via `extra_fields`, e.g. `{"customfield_10016": 5}` (story points).

## 5. Production notes

- Set `TOKEN_ENCRYPTION_KEY` and `SESSION_SECRET`. Switch `DATABASE_URL` to Postgres (`postgresql+asyncpg://…`, and add `asyncpg` to the requirements).
- With several app instances, move the SSE event bus ([app/events.py](app/events.py)) to Redis pub/sub, and run the webhook refresh loop ([app/scheduler.py](app/scheduler.py)) on only one instance.
- Webhook JQL filters support only `project`, `issueKey`, `issuetype`, `status`, `priority`, `assignee`, `reporter` with `=`, `!=`, `IN`, `NOT IN`. Webhook URLs must be HTTPS. Port 80 isn't allowed.
- Refresh tokens expire after 90 days of inactivity. When that happens, users get `not_connected` and must reconnect.
- Jira rate-limits per user. The client retries 429/503 responses and honours `Retry-After`.
