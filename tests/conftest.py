import os

import pytest
from tools import db as db_module

# Ensure the startup API key check passes when TestClient triggers the lifespan.
# Tests that call /evaluate monkeypatch evaluate_lead, so the key is never used.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    yield
