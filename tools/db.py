import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "leads.db"
DEDUP_TTL_HOURS = 24


@dataclass
class TenantConfig:
    client_id: str
    name: str
    budget_vip_min: int = 8_000_000
    budget_medium_min: int = 3_000_000
    currency: str = "EGP"
    vip_locations: list = field(default_factory=lambda: [
        "North Coast", "New Zayed", "Gouna", "Golden Square", "New Cairo"
    ])
    output_language: str = "en"
    monthly_quota: int = 1000
    is_active: bool = True
    sheets_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    wa_notify_url: str = ""
    wa_notify_token: str = ""
    wa_notify_to: str = ""
    webhook_url: str = ""
    vip_min_confidence: int = 70


DEFAULT_TENANT = TenantConfig(client_id="default", name="Default")


class QuotaExceededError(Exception):
    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(f"Monthly quota of {limit} evaluations reached (used: {used})")


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(table: str, column: str, definition: str) -> None:
    with get_conn() as conn:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                client_id         TEXT PRIMARY KEY,
                api_key_hash      TEXT NOT NULL UNIQUE,
                name              TEXT NOT NULL,
                budget_vip_min    INTEGER NOT NULL DEFAULT 8000000,
                budget_medium_min INTEGER NOT NULL DEFAULT 3000000,
                currency          TEXT NOT NULL DEFAULT 'EGP',
                vip_locations     TEXT NOT NULL DEFAULT 'North Coast,New Zayed,Gouna,Golden Square,New Cairo',
                output_language   TEXT NOT NULL DEFAULT 'en',
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id    TEXT NOT NULL,
                phone_hash   TEXT NOT NULL,
                tier         TEXT NOT NULL,
                confidence   INTEGER NOT NULL DEFAULT 0,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                evaluated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (client_id) REFERENCES tenants(client_id)
            );
            CREATE INDEX IF NOT EXISTS idx_eval_tenant_time
                ON evaluations (client_id, evaluated_at);
        """)
    # Idempotent column migrations — safe to run on every startup
    _add_column_if_missing("evaluations", "lead_name",         "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("evaluations", "phone_number",      "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("evaluations", "location",          "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("evaluations", "reasoning",         "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("evaluations", "source",            "TEXT NOT NULL DEFAULT 'api'")
    _add_column_if_missing("evaluations", "input_tokens",      "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("evaluations", "output_tokens",     "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("evaluations", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("tenants", "monthly_quota",      "INTEGER NOT NULL DEFAULT 1000")
    _add_column_if_missing("tenants", "is_active",          "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing("tenants", "sheets_id",          "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "telegram_bot_token", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "telegram_chat_id",   "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "wa_notify_url",      "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "wa_notify_token",    "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "wa_notify_to",       "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "webhook_url",        "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing("tenants", "vip_min_confidence", "INTEGER NOT NULL DEFAULT 70")
    _seed_from_env()


def _seed_from_env() -> None:
    raw = os.getenv("TENANT_SEED", "").strip()
    if not raw:
        return
    try:
        tenants = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("TENANT_SEED is set but contains invalid JSON — skipping auto-seed")
        return
    for t in tenants:
        try:
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO tenants (
                        client_id, api_key_hash, name,
                        budget_vip_min, budget_medium_min, currency,
                        vip_locations, output_language, monthly_quota,
                        is_active, sheets_id, telegram_bot_token,
                        telegram_chat_id, wa_notify_url, wa_notify_token,
                        wa_notify_to, webhook_url, vip_min_confidence
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        t["client_id"], t["api_key_hash"], t["name"],
                        t.get("budget_vip_min", 8_000_000),
                        t.get("budget_medium_min", 3_000_000),
                        t.get("currency", "EGP"),
                        t.get("vip_locations", "North Coast,New Zayed,Gouna,Golden Square,New Cairo"),
                        t.get("output_language", "en"),
                        t.get("monthly_quota", 1000),
                        t.get("is_active", 1),
                        t.get("sheets_id", ""),
                        t.get("telegram_bot_token", ""),
                        t.get("telegram_chat_id", ""),
                        t.get("wa_notify_url", ""),
                        t.get("wa_notify_token", ""),
                        t.get("wa_notify_to", ""),
                        t.get("webhook_url", ""),
                        t.get("vip_min_confidence", 70),
                    ),
                )
            log.info("seed: tenant %s ensured from TENANT_SEED", t["client_id"])
        except Exception:
            log.warning("seed: failed to insert tenant %s", t.get("client_id", "?"))


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def hash_phone(phone: str) -> str:
    return hashlib.sha256(phone.strip().encode()).hexdigest()


def get_tenant_by_api_key(api_key: str) -> TenantConfig | None:
    key_hash = hash_key(api_key)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE api_key_hash = ?", (key_hash,)
        ).fetchone()
    if row is None:
        return None
    return TenantConfig(
        client_id=row["client_id"],
        name=row["name"],
        budget_vip_min=row["budget_vip_min"],
        budget_medium_min=row["budget_medium_min"],
        currency=row["currency"],
        vip_locations=row["vip_locations"].split(","),
        output_language=row["output_language"],
        monthly_quota=row["monthly_quota"],
        is_active=bool(row["is_active"]),
        sheets_id=row["sheets_id"] or "",
        telegram_bot_token=row["telegram_bot_token"] or "",
        telegram_chat_id=row["telegram_chat_id"] or "",
        wa_notify_url=row["wa_notify_url"] or "",
        wa_notify_token=row["wa_notify_token"] or "",
        wa_notify_to=row["wa_notify_to"] or "",
        webhook_url=row["webhook_url"] or "",
        vip_min_confidence=row["vip_min_confidence"],
    )


def get_tenant_raw(client_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE client_id = ?", (client_id,)
        ).fetchone()
    return dict(row) if row else None


def is_duplicate(client_id: str, phone_hash: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_TTL_HOURS)).isoformat(sep=" ")
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM evaluations
               WHERE client_id = ? AND phone_hash = ? AND is_duplicate = 0
               AND evaluated_at > ?
               LIMIT 1""",
            (client_id, phone_hash, cutoff),
        ).fetchone()
    return row is not None


def log_evaluation(
    client_id: str,
    phone_hash: str,
    phone_number: str,
    lead_name: str,
    location: str,
    tier: str,
    confidence: int,
    reasoning: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    source: str = "api",
    is_dup: bool = False,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO evaluations (
                client_id, phone_hash, phone_number, lead_name, location,
                tier, confidence, reasoning, input_tokens, output_tokens,
                cache_read_tokens, source, is_duplicate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id, phone_hash, phone_number, lead_name, location,
                tier, confidence, reasoning[:500], input_tokens, output_tokens,
                cache_read_tokens, source, int(is_dup),
            ),
        )
        return cur.lastrowid


def create_tenant(client_id: str, api_key: str, name: str, **kwargs) -> None:
    key_hash = hash_key(api_key)
    vip_locs = kwargs.get(
        "vip_locations",
        ["North Coast", "New Zayed", "Gouna", "Golden Square", "New Cairo"],
    )
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tenants
               (client_id, api_key_hash, name, budget_vip_min, budget_medium_min,
                currency, vip_locations, output_language)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                key_hash,
                name,
                kwargs.get("budget_vip_min", 8_000_000),
                kwargs.get("budget_medium_min", 3_000_000),
                kwargs.get("currency", "EGP"),
                ",".join(vip_locs),
                kwargs.get("output_language", "en"),
            ),
        )


def update_tenant_fields(client_id: str, updates: dict) -> None:
    if not updates:
        return
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [client_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {cols} WHERE client_id = ?", vals)


def check_quota(client_id: str, monthly_quota: int) -> None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM evaluations
               WHERE client_id = ?
                 AND is_duplicate = 0
                 AND evaluated_at >= date('now', 'start of month')""",
            (client_id,),
        ).fetchone()
    used = row["cnt"]
    if used >= monthly_quota:
        raise QuotaExceededError(used, monthly_quota)


def get_quota_status(client_id: str, monthly_quota: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM evaluations
               WHERE client_id = ?
                 AND is_duplicate = 0
                 AND evaluated_at >= date('now', 'start of month')""",
            (client_id,),
        ).fetchone()
    used = row["cnt"]
    return {"used": used, "limit": monthly_quota, "remaining": max(0, monthly_quota - used)}


def get_stats_window(client_id: str, hours: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(sep=" ")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN tier = 'VIP'    AND is_duplicate = 0 THEN 1 ELSE 0 END) AS vip,
                SUM(CASE WHEN tier = 'Medium' AND is_duplicate = 0 THEN 1 ELSE 0 END) AS medium,
                SUM(CASE WHEN tier = 'Low'    AND is_duplicate = 0 THEN 1 ELSE 0 END) AS low,
                SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END)                     AS duplicates,
                AVG(CASE WHEN is_duplicate = 0 THEN confidence ELSE NULL END)          AS avg_conf,
                SUM(CASE WHEN is_duplicate = 0 THEN input_tokens      ELSE 0 END)     AS input_tokens,
                SUM(CASE WHEN is_duplicate = 0 THEN output_tokens     ELSE 0 END)     AS output_tokens,
                SUM(CASE WHEN is_duplicate = 0 THEN cache_read_tokens ELSE 0 END)     AS cache_read_tokens
            FROM evaluations
            WHERE client_id = ? AND evaluated_at > ?
            """,
            (client_id, cutoff),
        ).fetchone()
    avg_conf = row["avg_conf"]
    input_t  = row["input_tokens"] or 0
    output_t = row["output_tokens"] or 0
    cache_t  = row["cache_read_tokens"] or 0
    estimated_usd = round(
        (input_t * 0.000003) + (output_t * 0.000015) - (cache_t * 0.0000027), 4
    )
    return {
        "total":             row["total"] or 0,
        "vip":               row["vip"] or 0,
        "medium":            row["medium"] or 0,
        "low":               row["low"] or 0,
        "duplicates":        row["duplicates"] or 0,
        "avg_confidence":    int(avg_conf + 0.5) if avg_conf is not None else 0,
        "input_tokens":      input_t,
        "output_tokens":     output_t,
        "cache_read_tokens": cache_t,
        "estimated_usd":     estimated_usd,
    }


def get_top_locations(client_id: str, hours: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(sep=" ")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT location, COUNT(*) AS count
               FROM evaluations
               WHERE client_id = ? AND is_duplicate = 0
                 AND evaluated_at > ? AND location != ''
               GROUP BY location
               ORDER BY count DESC
               LIMIT 5""",
            (client_id, cutoff),
        ).fetchall()
    return [{"location": r["location"], "count": r["count"]} for r in rows]


def get_recent_evaluations(client_id: str, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT lead_name, tier, confidence, location, evaluated_at, is_duplicate,
                      '****' || SUBSTR(phone_number, -4) AS phone_masked
               FROM evaluations
               WHERE client_id = ?
               ORDER BY evaluated_at DESC
               LIMIT ?""",
            (client_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
