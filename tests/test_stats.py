import sqlite3
import pytest
from datetime import datetime, timedelta, timezone
from tools import db as db_module
from tools.db import (
    create_tenant,
    get_stats_window,
    hash_phone,
    log_evaluation,
)


def test_empty_window_returns_zeroed_counts(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    stats = get_stats_window("agency-01", 24)
    assert stats == {
        "total": 0, "vip": 0, "medium": 0, "low": 0,
        "duplicates": 0, "avg_confidence": 0,
    }


def test_tier_counts_exclude_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 90)
    log_evaluation("agency-01", hash_phone("+2"), "Medium", 70)
    log_evaluation("agency-01", hash_phone("+3"), "Low", 40)
    log_evaluation("agency-01", hash_phone("+4"), "VIP", 0, is_dup=True)
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
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 88)
    stats = get_stats_window("agency-02", 24)
    assert stats["total"] == 0


def test_avg_confidence_rounds_correctly(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)
    log_evaluation("agency-01", hash_phone("+2"), "Medium", 90)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 85


def test_avg_confidence_excludes_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)
    log_evaluation("agency-01", hash_phone("+2"), "Medium", 0, is_dup=True)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 80


def test_avg_confidence_classical_rounding(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)
    log_evaluation("agency-01", hash_phone("+2"), "Medium", 81)
    # avg = 80.5 — classical rounding gives 81, banker's rounding gives 80
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 81


def test_avg_confidence_zero_when_all_duplicates(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 0, is_dup=True)
    stats = get_stats_window("agency-01", 24)
    assert stats["avg_confidence"] == 0


def test_hours_zero_returns_zeroed_counts(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 90)
    # hours=0 means cutoff=now, so evaluated_at > now is always false
    stats = get_stats_window("agency-01", 0)
    assert stats["total"] == 0
