import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from tools import db as db_module
from tools.db import (
    create_tenant,
    get_stats_window,
    hash_phone,
    init_db,
    log_evaluation,
)


def _log(client_id, phone, tier, confidence, is_dup=False):
    log_evaluation(
        client_id, hash_phone(phone), phone, "Test Lead", "North Coast",
        tier, confidence, "reasoning", 100, 50, 80, is_dup=is_dup,
    )


def test_empty_window_returns_zeroed_counts(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    stats = get_stats_window("agency-01", 24)
    assert stats["total"] == 0
    assert stats["vip"] == 0
    assert stats["medium"] == 0
    assert stats["low"] == 0
    assert stats["duplicates"] == 0
    assert stats["avg_confidence"] == 0
    assert stats["input_tokens"] == 0
    assert stats["output_tokens"] == 0
    assert stats["estimated_usd"] == 0.0


def test_tier_counts_exclude_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 90)
    _log("agency-01", "+2", "Medium", 70)
    _log("agency-01", "+3", "Low", 40)
    _log("agency-01", "+4", "VIP", 0, is_dup=True)
    stats = get_stats_window("agency-01", 24)
    assert stats["vip"] == 1
    assert stats["medium"] == 1
    assert stats["low"] == 1
    assert stats["duplicates"] == 1
    assert stats["total"] == 4


def test_records_outside_window_excluded(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(sep=" ")
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        """INSERT INTO evaluations
           (client_id, phone_hash, tier, confidence, is_duplicate, evaluated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("agency-01", hash_phone("+1"), "VIP", 90, 0, cutoff),
    )
    conn.commit()
    conn.close()
    stats = get_stats_window("agency-01", 24)
    assert stats["total"] == 0
    assert stats["vip"] == 0


def test_stats_isolated_per_tenant(tmp_db):
    create_tenant("agency-01", "key1", "Alpha")
    create_tenant("agency-02", "key2", "Beta")
    _log("agency-01", "+1", "VIP", 88)
    stats = get_stats_window("agency-02", 24)
    assert stats["total"] == 0


def test_avg_confidence_rounds_correctly(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 80)
    _log("agency-01", "+2", "Medium", 90)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 85


def test_avg_confidence_excludes_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 80)
    _log("agency-01", "+2", "Medium", 0, is_dup=True)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 80


def test_avg_confidence_classical_rounding(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 80)
    _log("agency-01", "+2", "Medium", 81)
    # avg = 80.5 — classical rounding gives 81, banker's rounding gives 80
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 81


def test_avg_confidence_zero_when_all_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 0, is_dup=True)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 0


def test_hours_zero_returns_zeroed_counts(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    _log("agency-01", "+1", "VIP", 90)
    # hours=0 means cutoff=now, so evaluated_at > now is always false
    stats = get_stats_window("agency-01", 0)
    assert stats["total"] == 0


# ---------------------------------------------------------------------------
# Integration tests — GET /stats endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()
    create_tenant("agency-01", "valid-key", "Alpha Realty")
    from tools.api_server import app
    with TestClient(app) as c:
        yield c


def test_stats_returns_200_with_all_windows(client):
    resp = client.get("/stats", headers={"X-API-Key": "valid-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["client_id"] == "agency-01"
    assert set(data["windows"].keys()) == {"last_24h", "last_7d", "last_30d"}
    assert "quota" in data
    assert data["quota"]["limit"] == 1000


def test_stats_missing_key_returns_422(client):
    # FastAPI returns 422 (Unprocessable Entity) for a missing required Header(...)
    # parameter — this is consistent with test_auth.py::test_missing_api_key_returns_422
    resp = client.get("/stats")
    assert resp.status_code == 422


def test_stats_invalid_key_returns_401(client):
    resp = client.get("/stats", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_stats_empty_db_returns_zeroed_counts(client):
    resp = client.get("/stats", headers={"X-API-Key": "valid-key"})
    window = resp.json()["windows"]["last_24h"]
    assert window["total"] == 0
    assert window["vip"] == 0
    assert window["avg_confidence"] == 0
    assert window["tokens"]["input"] == 0
    assert window["tokens"]["estimated_usd"] == 0.0


def test_stats_counts_reflect_evaluations(client):
    _log("agency-01", "+1", "VIP", 90)
    _log("agency-01", "+2", "VIP", 0, is_dup=True)
    resp = client.get("/stats", headers={"X-API-Key": "valid-key"})
    w = resp.json()["windows"]["last_24h"]
    assert w["vip"] == 1
    assert w["duplicates"] == 1
    assert w["total"] == 2
    assert w["avg_confidence"] == 90  # duplicate excluded from avg


def test_stats_cross_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "isolated.db")
    init_db()
    create_tenant("agency-01", "key-one", "Alpha")
    create_tenant("agency-02", "key-two", "Beta")
    _log("agency-01", "+1", "VIP", 88)
    from tools.api_server import app
    with TestClient(app) as c:
        resp = c.get("/stats", headers={"X-API-Key": "key-two"})
    assert resp.status_code == 200
    assert resp.json()["windows"]["last_24h"]["total"] == 0
