import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_db_file = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_file.as_posix()}"
os.environ["ATLASSIAN_CLIENT_ID"] = "test-client"
os.environ["ATLASSIAN_CLIENT_SECRET"] = "test-secret"
os.environ["APP_BASE_URL"] = "https://example.test"
os.environ["SESSION_SECRET"] = "test-session-secret"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.db import Base, engine  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"
