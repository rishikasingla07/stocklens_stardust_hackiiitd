"""Accounts, sessions, per-user watchlist and history, catalog. Temporary DB per test (conftest)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import app as app_module
from backend import config
from backend.db import users
from tests.conftest import TEST_ACCOUNT

client = TestClient(app_module.app)  # logged in as TEST_ACCOUNT by the conftest fixture
COOKIE = config.AUTH_COOKIE_NAME


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(config, "MODE", "replay")
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def new_client(account_id: str | None = None, password: str = "secret-pw-1") -> TestClient:
    """A separate browser; signed up as account_id if given."""
    c = TestClient(app_module.app)
    if account_id:
        r = c.post("/auth/signup", json={"account_id": account_id, "password": password})
        assert r.status_code == 201, r.text
    return c


# --- sign-up -----------------------------------------------------------------------

def test_signup_logs_in_with_httponly_cookie(anon_client):
    r = anon_client.post("/auth/signup", json={"account_id": "asha", "password": "pw-123456"})
    assert r.status_code == 201 and r.json() == {"account_id": "asha"}
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert anon_client.get("/me").json() == {"account_id": "asha"}


def test_duplicate_account_id_is_rejected_case_insensitively(anon_client):
    r = anon_client.post("/auth/signup", json={"account_id": TEST_ACCOUNT[0].upper(), "password": "another1"})
    assert r.status_code == 409 and "already taken" in r.json()["detail"]


@pytest.mark.parametrize("account_id, password", [
    ("ab", "longenough"),            # ID too short
    ("a" * 33, "longenough"),        # ID too long
    ("has space", "longenough"),     # bad characters
    ("<script>", "longenough"),
    ("valid_id", "12345"),           # password too short
])
def test_signup_validation(anon_client, account_id, password):
    r = anon_client.post("/auth/signup", json={"account_id": account_id, "password": password})
    assert r.status_code == 422
    assert password not in r.text  # the 422 body never echoes what was sent
    assert anon_client.get("/me").status_code == 401


def test_validation_error_does_not_echo_password(anon_client):
    r = anon_client.post("/auth/signup", json={"account_id": "x", "password": "my-secret-pw"})
    assert r.status_code == 422 and "my-secret-pw" not in r.text


# --- login / logout ------------------------------------------------------------------

def test_login_success(anon_client):
    r = anon_client.post("/auth/login", json={"account_id": TEST_ACCOUNT[0], "password": TEST_ACCOUNT[1]})
    assert r.status_code == 200 and r.json() == {"account_id": TEST_ACCOUNT[0]}
    assert anon_client.get("/me").status_code == 200


def test_login_failure_is_the_same_for_unknown_id_and_wrong_password(anon_client):
    wrong = anon_client.post("/auth/login", json={"account_id": TEST_ACCOUNT[0], "password": "wrong-pw"})
    unknown = anon_client.post("/auth/login", json={"account_id": "nobody-here", "password": "wrong-pw"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Account ID or password is incorrect."}
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers
    assert anon_client.get("/me").status_code == 401


def test_logout_ends_the_session():
    token = client.cookies.get(COOKIE)
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/me").status_code == 401
    replay = TestClient(app_module.app)
    replay.cookies.set(COOKIE, token)  # the old token is dead on the server too
    assert replay.get("/me").status_code == 401


def test_expired_session_is_rejected(temp_db):
    with sqlite3.connect(temp_db) as conn:
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+05:30'")
    assert client.get("/me").status_code == 401


def test_garbage_cookie_is_rejected(anon_client):
    anon_client.cookies.set(COOKIE, "not-a-real-token")
    assert anon_client.get("/watchlist").status_code == 401


# --- protected routes ------------------------------------------------------------------

PROTECTED = [
    ("GET", "/me", None), ("GET", "/stocks", None), ("GET", "/cases", None),
    ("GET", "/catalog", None), ("GET", "/watchlist", None), ("PUT", "/watchlist", {"tickers": []}),
    ("POST", "/investigate/ACMEINFRA", None), ("GET", "/receipt/ACMEINFRA", None),
    ("GET", "/receipt/ACMEINFRA?format=html", None), ("POST", "/simulate/ACMEINFRA", None),
    ("GET", "/live/status", None), ("GET", "/live/somejob", None),
    ("POST", "/live/start", {"company": "X", "ticker": "X", "move_start_at": "2026-10-22T10:00:00",
                             "pct_change": 1, "volume_multiple": 2, "sector_pct_change": 0,
                             "index_pct_change": 0}),
    ("GET", "/history", None), ("GET", "/history/1", None),
]


@pytest.mark.parametrize("method, path, body", PROTECTED)
def test_protected_routes_need_a_session(anon_client, method, path, body):
    r = anon_client.request(method, path, json=body)
    assert r.status_code == 401
    assert r.json()["detail"] == app_module.NOT_LOGGED_IN


@pytest.mark.parametrize("path", ["/health", "/", "/static/app.js", "/auth/session"])
def test_public_routes_stay_open(anon_client, path):
    assert anon_client.get(path).status_code == 200


def test_session_check_says_who_is_logged_in(anon_client):
    assert anon_client.get("/auth/session").json() == {"account_id": None}
    assert client.get("/auth/session").json() == {"account_id": TEST_ACCOUNT[0]}


# --- per-user data -----------------------------------------------------------------------

def test_watchlist_is_per_user():
    assert client.get("/watchlist").json() == {"tickers": [], "stocks": []}  # new account: empty
    saved = client.put("/watchlist", json={"tickers": ["tcs", "ACMEINFRA", "TCS"]}).json()
    assert saved["tickers"] == ["TCS", "ACMEINFRA"]  # upper-cased, de-duplicated, order kept
    other = new_client("other-user")
    assert other.get("/watchlist").json()["tickers"] == []
    other.put("/watchlist", json={"tickers": ["INFY"]})
    assert client.get("/watchlist").json()["tickers"] == ["TCS", "ACMEINFRA"]
    assert other.get("/watchlist").json()["tickers"] == ["INFY"]


def test_watchlist_rejects_unknown_tickers():
    r = client.put("/watchlist", json={"tickers": ["ACMEINFRA", "NOTREAL"]})
    assert r.status_code == 422 and "NOTREAL" in r.json()["detail"]
    assert client.get("/watchlist").json()["tickers"] == []  # nothing half-saved


def test_watchlist_rows_only_price_demo_coverage_stocks():
    rows = {r["ticker"]: r for r in client.put(
        "/watchlist", json={"tickers": ["ACMEINFRA", "RELIANCE"]}).json()["stocks"]}
    assert rows["ACMEINFRA"]["demo_coverage"] and rows["ACMEINFRA"]["alert"] is True
    assert rows["ACMEINFRA"]["price"] == 1180.0  # recorded demo feed
    reliance = rows["RELIANCE"]
    assert not reliance["demo_coverage"] and reliance["alert"] is False
    assert reliance["price"] is None and reliance["change_pct"] is None  # no invented prices


def test_history_is_per_user():
    client.post("/investigate/ACMEINFRA")
    [mine] = client.get("/history").json()
    other = new_client("history-peeker")
    assert other.get("/history").json() == []
    assert other.get(f"/history/{mine['id']}").status_code == 404  # cannot open someone else's
    assert client.get(f"/history/{mine['id']}").status_code == 200


# --- passwords and tokens at rest ------------------------------------------------------------

def test_passwords_and_tokens_are_never_stored_in_plain_text(temp_db):
    password = "Very-Distinctive-Pa55word"
    c = new_client("plaintext-check", password)
    token = c.cookies.get(COOKIE)
    raw = temp_db.read_bytes()
    assert password.encode() not in raw
    assert token.encode() not in raw  # only the token's SHA-256 is stored
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT password_hash, salt, iterations FROM users WHERE account_id = ?",
                           ("plaintext-check",)).fetchone()
    password_hash, salt, iterations = row
    assert len(password_hash) == 64 and len(salt) == 2 * config.AUTH_SALT_BYTES
    assert password_hash == users.hash_password(password, bytes.fromhex(salt), iterations)


def test_same_password_gets_different_hashes(temp_db):
    new_client("twin-one", "same-password")
    new_client("twin-two", "same-password")
    with sqlite3.connect(temp_db) as conn:
        hashes = [r[0] for r in conn.execute(
            "SELECT password_hash FROM users WHERE account_id IN ('twin-one', 'twin-two')")]
    assert len(set(hashes)) == 2  # random salt per user


def test_password_never_in_any_response(anon_client):
    password = "Echo-Me-Not-123"
    texts = [anon_client.post("/auth/signup", json={"account_id": "echo-check", "password": password}).text,
             anon_client.get("/me").text,
             anon_client.post("/auth/login", json={"account_id": "echo-check", "password": password}).text,
             anon_client.post("/auth/login", json={"account_id": "echo-check", "password": password + "x"}).text]
    assert all(password not in t for t in texts)


# --- demo account --------------------------------------------------------------------------

def test_demo_account_seeded_at_startup_in_replay_mode(anon_client):
    with TestClient(app_module.app) as started:  # runs startup
        r = started.post("/auth/login", json={"account_id": "demo", "password": "demo123"})
        assert r.status_code == 200
        assert started.get("/watchlist").json()["tickers"] == list(config.DEMO_WATCHLIST)


def test_demo_account_not_created_in_live_mode(monkeypatch):
    monkeypatch.setattr(config, "MODE", "live")
    with TestClient(app_module.app):
        pass
    assert users.find_user("demo") is None


def test_demo_account_cannot_log_in_in_live_mode(monkeypatch):
    users.ensure_demo_account()
    monkeypatch.setattr(config, "MODE", "live")
    c = TestClient(app_module.app)
    r = c.post("/auth/login", json={"account_id": "demo", "password": "demo123"})
    assert r.status_code == 401 and r.json()["detail"] == "Account ID or password is incorrect."


def test_startup_does_not_reset_an_existing_demo_watchlist():
    users.ensure_demo_account()
    demo = users.find_user("demo")
    users.set_watchlist(demo.id, ["TCS"])
    assert users.ensure_demo_account() is False
    assert users.get_watchlist(demo.id) == ["TCS"]


# --- catalog -----------------------------------------------------------------------------

def test_catalog_lists_demo_coverage_first():
    stocks = client.get("/catalog").json()
    covered = [s["ticker"] for s in stocks if s["demo_coverage"]]
    # The three recorded cases plus LOTUSFOODS, the quiet ticker that has demo price history only.
    assert set(covered) == {"ACMEINFRA", "ZENITHPOLY", "ORIONPHARMA", "LOTUSFOODS"}
    assert [s["demo_coverage"] for s in stocks[:4]] == [True] * 4
    assert len(stocks) - 4 >= 20
    for s in stocks:
        assert set(s) == {"ticker", "name", "sector", "demo_coverage", "fictional"}
        assert "price" not in s


def test_investigating_a_stock_without_coverage_says_so():
    r = client.post("/investigate/RELIANCE")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("No investigation available in this demo")
    assert client.get("/history").json() == []
