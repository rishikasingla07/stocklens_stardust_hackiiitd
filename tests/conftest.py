"""Shared test setup.

- Every test uses a temporary SQLite file, never the real stocklens.db.
- Test modules with a module-level `client = TestClient(app)` are logged in
  through the real POST /auth/signup route before each test, as TEST_ACCOUNT.
  Use `anon_client` for requests without a session.
"""

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.db import users

TEST_ACCOUNT = ("tester", "tester-password")


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test-history.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    # Far fewer pbkdf2 rounds than production so the suite stays fast; the
    # iteration count is stored per hash, so this does not change the code path.
    monkeypatch.setattr(config, "AUTH_PBKDF2_ITERATIONS", 1_000)
    return path


@pytest.fixture(autouse=True)
def logged_in(request, temp_db):
    """Sign the module's `client` up and in as TEST_ACCOUNT (fresh database, so a fresh account)."""
    client = getattr(request.module, "client", None)
    if not isinstance(client, TestClient):
        yield None
        return
    client.cookies.clear()
    r = client.post("/auth/signup", json={"account_id": TEST_ACCOUNT[0], "password": TEST_ACCOUNT[1]})
    assert r.status_code == 201, r.text
    yield users.find_user(TEST_ACCOUNT[0])
    client.cookies.clear()


@pytest.fixture
def test_user(logged_in):
    """The users.User that the module's client is logged in as."""
    return logged_in


@pytest.fixture
def anon_client():
    from backend import app as app_module
    return TestClient(app_module.app)
