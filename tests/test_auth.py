import pytest
from fastapi.testclient import TestClient
from tools import db as db_module
from tools.db import create_tenant, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()
    create_tenant("agency-01", "valid-key-xyz", "Alpha Realty")
    from tools.api_server import app
    with TestClient(app) as c:
        yield c


def test_missing_api_key_returns_422(client):
    resp = client.post("/evaluate", json={"lead_name": "Test"})
    assert resp.status_code == 422


def test_invalid_api_key_returns_401(client):
    resp = client.post(
        "/evaluate",
        headers={"X-API-Key": "wrong-key"},
        json={"lead_name": "Test"},
    )
    assert resp.status_code == 401


def test_valid_api_key_passes_auth(client, monkeypatch):
    monkeypatch.setattr(
        "tools.api_server.evaluate_lead",
        lambda lead, tenant: {
            "tier": "Medium",
            "confidence": 70,
            "reasoning": "Standard profile.",
            "visual_signals": "none",
            "sales_strategy": "Add to standard pipeline.",
        },
    )
    resp = client.post(
        "/evaluate",
        headers={"X-API-Key": "valid-key-xyz"},
        json={"lead_name": "Ahmed"},
    )
    assert resp.status_code == 200
    assert resp.json()["tier"] == "Medium"
    assert resp.json()["confidence"] == 70
    assert resp.json()["is_duplicate"] is False


def test_duplicate_phone_returns_without_calling_claude(client, monkeypatch):
    from tools.db import hash_phone, log_evaluation
    phone = "+201234567890"
    phone_hash = hash_phone(phone)
    log_evaluation("agency-01", phone_hash, "VIP", 90)

    claude_called = []
    monkeypatch.setattr(
        "tools.api_server.evaluate_lead",
        lambda lead, tenant: claude_called.append(True) or {},
    )

    resp = client.post(
        "/evaluate",
        headers={"X-API-Key": "valid-key-xyz"},
        json={"lead_name": "Ahmed", "phone_number": phone},
    )
    assert resp.status_code == 200
    assert resp.json()["is_duplicate"] is True
    assert claude_called == []


def test_dedup_is_isolated_per_tenant(tmp_path, monkeypatch):
    # Fresh DB — isolated from the `client` fixture's DB
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "isolated.db")
    init_db()
    create_tenant("agency-01", "key-one", "Alpha")
    create_tenant("agency-02", "key-two", "Beta")

    from tools.db import hash_phone, log_evaluation
    phone = "+201111111111"
    log_evaluation("agency-01", hash_phone(phone), "VIP", 88)

    from tools.api_server import app
    monkeypatch.setattr(
        "tools.api_server.evaluate_lead",
        lambda lead, tenant: {
            "tier": "Medium",
            "confidence": 55,
            "reasoning": "ok",
            "visual_signals": "none",
            "sales_strategy": "pipeline",
        },
    )
    with TestClient(app) as c:
        resp = c.post(
            "/evaluate",
            headers={"X-API-Key": "key-two"},
            json={"lead_name": "Sara", "phone_number": phone},
        )
    assert resp.status_code == 200
    assert resp.json()["is_duplicate"] is False


def test_health_endpoint_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_suspended_tenant_returns_403(tmp_path, monkeypatch):
    import sqlite3 as _sqlite3
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()
    create_tenant("agency-suspended", "suspended-key", "Suspended Agency")
    conn = _sqlite3.connect(str(db_module.DB_PATH))
    conn.execute("UPDATE tenants SET is_active = 0 WHERE client_id = 'agency-suspended'")
    conn.commit()
    conn.close()
    from tools.api_server import app
    with TestClient(app) as c:
        resp = c.post(
            "/evaluate",
            headers={"X-API-Key": "suspended-key"},
            json={"lead_name": "Test"},
        )
    assert resp.status_code == 403
    assert "suspended" in resp.json()["detail"].lower()
