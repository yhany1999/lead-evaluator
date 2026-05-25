import sqlite3
import pytest
from tools import db as db_module
from tools.db import (
    create_tenant,
    get_tenant_by_api_key,
    hash_phone,
    is_duplicate,
    log_evaluation,
)


def test_get_tenant_returns_none_for_unknown_key(tmp_db):
    assert get_tenant_by_api_key("nonexistent-key") is None


def test_create_and_lookup_tenant(tmp_db):
    create_tenant("agency-01", "secret-key-abc", "Alpha Realty")
    tenant = get_tenant_by_api_key("secret-key-abc")
    assert tenant is not None
    assert tenant.client_id == "agency-01"
    assert tenant.name == "Alpha Realty"
    assert tenant.currency == "EGP"
    assert isinstance(tenant.vip_locations, list)
    assert len(tenant.vip_locations) > 0


def test_create_tenant_with_custom_config(tmp_db):
    create_tenant(
        "agency-ae",
        "key-ae",
        "Dubai Properties",
        budget_vip_min=2_000_000,
        currency="AED",
        vip_locations=["Palm Jumeirah", "Downtown Dubai"],
        output_language="ar",
    )
    tenant = get_tenant_by_api_key("key-ae")
    assert tenant.budget_vip_min == 2_000_000
    assert tenant.currency == "AED"
    assert "Palm Jumeirah" in tenant.vip_locations
    assert tenant.output_language == "ar"


def test_duplicate_api_key_raises(tmp_db):
    create_tenant("a1", "same-key", "A")
    with pytest.raises(Exception):
        create_tenant("a2", "same-key", "B")


def test_is_duplicate_false_for_new_phone(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    phone_hash = hash_phone("+201234567890")
    assert is_duplicate("agency-01", phone_hash) is False


def test_is_duplicate_true_after_evaluation(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    phone_hash = hash_phone("+201234567890")
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 92, "reasoning", 100, 50, 80)
    assert is_duplicate("agency-01", phone_hash) is True


def test_is_duplicate_false_after_ttl(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    phone_hash = hash_phone("+201234567890")
    # Insert a record with a timestamp 25 hours ago — past the 24h TTL
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        """INSERT INTO evaluations (client_id, phone_hash, tier, confidence, is_duplicate, evaluated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now', '-25 hours'))""",
        ("agency-01", phone_hash, "Medium", 60, 0),
    )
    conn.commit()
    conn.close()
    assert is_duplicate("agency-01", phone_hash) is False


def test_is_duplicate_isolated_per_client(tmp_db):
    create_tenant("agency-01", "key1", "Alpha")
    create_tenant("agency-02", "key2", "Beta")
    phone_hash = hash_phone("+201234567890")
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 88, "reasoning", 100, 50, 80)
    # Same phone, different client — must NOT be a duplicate
    assert is_duplicate("agency-02", phone_hash) is False


def test_log_evaluation_records_entry(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    phone_hash = hash_phone("+201234567890")
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 90, "reasoning", 100, 50, 80)
    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute(
        "SELECT tier, confidence FROM evaluations WHERE client_id = ?", ("agency-01",)
    ).fetchone()
    conn.close()
    assert row[0] == "VIP"
    assert row[1] == 90
