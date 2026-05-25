import pytest
from tools.db import (
    QuotaExceededError,
    check_quota,
    create_tenant,
    get_quota_status,
    hash_phone,
    log_evaluation,
)


def _log(client_id, phone, tier, confidence, is_dup=False):
    log_evaluation(
        client_id, hash_phone(phone), phone, "Test Lead", "North Coast",
        tier, confidence, "Test reasoning.", 100, 50, 80, is_dup=is_dup,
    )


def test_check_quota_passes_below_limit(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    for i in range(5):
        _log("agency-01", f"+20100000{i:04d}", "VIP", 80)
    check_quota("agency-01", 10)  # 5 < 10 — must not raise


def test_check_quota_raises_at_limit(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    for i in range(10):
        _log("agency-01", f"+20100000{i:04d}", "VIP", 80)
    with pytest.raises(QuotaExceededError) as exc_info:
        check_quota("agency-01", 10)
    assert exc_info.value.used == 10
    assert exc_info.value.limit == 10


def test_check_quota_excludes_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    for i in range(10):
        _log("agency-01", f"+20100000{i:04d}", "VIP", 80, is_dup=True)
    check_quota("agency-01", 10)  # all duplicates — real count is 0, must not raise


def test_get_quota_status_correct_values(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    for i in range(3):
        _log("agency-01", f"+20100000{i:04d}", "VIP", 80)
    status = get_quota_status("agency-01", 100)
    assert status["used"] == 3
    assert status["limit"] == 100
    assert status["remaining"] == 97


def test_get_quota_status_remaining_never_negative(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    for i in range(15):
        _log("agency-01", f"+20100000{i:04d}", "VIP", 80)
    status = get_quota_status("agency-01", 10)
    assert status["remaining"] == 0  # clamped, not negative


def test_quota_api_returns_429_when_exceeded(tmp_path, monkeypatch):
    from tools import db as db_module
    from tools.db import init_db, create_tenant
    from fastapi.testclient import TestClient
    import sqlite3

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()
    create_tenant("agency-q", "quota-key", "Quota Agency")
    conn = sqlite3.connect(str(db_module.DB_PATH))
    for i in range(1000):
        conn.execute(
            """INSERT INTO evaluations
               (client_id, phone_hash, phone_number, lead_name, location,
                tier, confidence, is_duplicate)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            ("agency-q", f"hash{i}", f"+2010000{i:04d}", "Lead", "NC", "VIP", 80),
        )
    conn.commit()
    conn.close()

    from tools.api_server import app
    with TestClient(app) as c:
        resp = c.post(
            "/evaluate",
            headers={"X-API-Key": "quota-key"},
            json={"lead_name": "Test"},
        )
    assert resp.status_code == 429
    assert "quota" in resp.json()["detail"].lower()
