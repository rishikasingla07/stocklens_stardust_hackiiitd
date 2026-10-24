"""Shared test setup: every test uses a temporary SQLite file, never the real stocklens.db."""

import pytest

from backend import config


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "test-history.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    return path
