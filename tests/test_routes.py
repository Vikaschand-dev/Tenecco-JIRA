"""Route-level tests with the Jira client's transport mocked out."""

from fastapi.testclient import TestClient

from app.deps import get_jira
from app.main import app


class FakeJira:
    cloud_id = "cloud-123"

    def __init__(self):
        self.calls = []
        self.status = "To Do"

    async def get_issue(self, key, fields="*navigable", expand=None):
        return {
            "id": "1",
            "key": key,
            "fields": {
                "summary": "Login broken",
                "status": {"name": self.status, "statusCategory": {"key": "new"}},
                "issuetype": {"name": "Bug"},
                "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Steps"}]}]},
                "labels": ["web"],
            },
        }

    async def create_issue(self, **kw):
        self.calls.append(("create", kw))
        return {"id": "1", "key": "ABC-1"}

    async def update_issue(self, key, **kw):
        self.calls.append(("update", key, kw))

    async def change_status(self, key, status, comment=None):
        self.calls.append(("status", key, status, comment))
        self.status = status

    async def transition_issue(self, key, tid, comment=None):
        self.calls.append(("transition", key, tid))


def _client(fake):
    app.dependency_overrides[get_jira] = lambda: fake
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_create_issue_returns_simplified_issue():
    fake = FakeJira()
    with _client(fake) as c:
        r = c.post("/api/jira/issues", json={"project_key": "ABC", "summary": "Login broken", "issue_type": "Bug", "description": "Steps"})
    assert r.status_code == 201
    body = r.json()
    assert body["key"] == "ABC-1" and body["description"] == "Steps" and body["issue_type"] == "Bug"
    assert fake.calls[0][1]["project_key"] == "ABC"


def test_update_issue():
    fake = FakeJira()
    with _client(fake) as c:
        r = c.patch("/api/jira/issues/ABC-1", json={"summary": "New title", "labels": ["a"]})
    assert r.status_code == 200
    assert fake.calls == [("update", "ABC-1", {"summary": "New title", "description": None, "priority": None, "labels": ["a"], "extra_fields": None, "notify_users": True})]


def test_change_status_by_name():
    fake = FakeJira()
    with _client(fake) as c:
        r = c.post("/api/jira/issues/ABC-1/status", json={"status": "Done", "comment": "Fixed"})
    assert r.json()["status"] == "Done"
    assert fake.calls == [("status", "ABC-1", "Done", "Fixed")]


def test_change_status_requires_target():
    with _client(FakeJira()) as c:
        assert c.post("/api/jira/issues/ABC-1/status", json={}).status_code == 400


def test_board_columns_ordered_and_issues_returned():
    fake = FakeJira()

    async def project_statuses(key):
        return [
            {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
            {"id": "3", "name": "Done", "statusCategory": {"key": "done"}},
            {"id": "4", "name": "Testing", "statusCategory": {"key": "indeterminate"}},
            {"id": "2", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        ]

    async def search_all(jql, fields=None, limit=1000):
        fake.calls.append(("search_all", jql))
        return [{"id": "1", "key": "APL-1", "fields": {"summary": "S", "status": {"name": "Testing"}, "parent": {"key": "APL-0"}}}]

    fake.project_statuses = project_statuses
    fake.search_all = search_all
    with _client(fake) as c:
        r = c.get("/api/jira/projects/APL/board", params={"jql": "assignee = currentUser()"})
    body = r.json()
    assert [col["name"] for col in body["columns"]] == ["To Do", "In Progress", "Testing", "Done"]
    assert body["issues"][0]["key"] == "APL-1" and body["issues"][0]["parent"] == "APL-0"
    assert fake.calls[0][1].startswith('project = "APL" AND (assignee = currentUser())')


def test_api_requires_connection():
    with TestClient(app) as c:
        r = c.get("/api/jira/projects")
    assert r.status_code == 401
