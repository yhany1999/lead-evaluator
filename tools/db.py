import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


DEFAULT_TENANT = TenantConfig(client_id="default", name="Default")


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


def init_db() -> None:
    with get_conn() as conn:
        # WAL mode persists on the DB file; safe to set on every startup.
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
    )


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
    client_id: str, phone_hash: str, tier: str, confidence: int, is_dup: bool = False
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO evaluations (client_id, phone_hash, tier, confidence, is_duplicate)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, phone_hash, tier, confidence, int(is_dup)),
        )


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


def get_stats_window(client_id: str, hours: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(sep=" ")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,  -- includes both real evaluations and duplicates
                SUM(CASE WHEN tier = 'VIP'    AND is_duplicate = 0 THEN 1 ELSE 0 END) AS vip,
                SUM(CASE WHEN tier = 'Medium' AND is_duplicate = 0 THEN 1 ELSE 0 END) AS medium,
                SUM(CASE WHEN tier = 'Low'    AND is_duplicate = 0 THEN 1 ELSE 0 END) AS low,
                SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END)                     AS duplicates,
                AVG(CASE WHEN is_duplicate = 0 THEN confidence ELSE NULL END)          AS avg_conf
            FROM evaluations
            WHERE client_id = ? AND evaluated_at > ?
            """,
            (client_id, cutoff),
        ).fetchone()
    avg_conf = row["avg_conf"]
    return {
        "total":          row["total"] or 0,
        "vip":            row["vip"] or 0,
        "medium":         row["medium"] or 0,
        "low":            row["low"] or 0,
        "duplicates":     row["duplicates"] or 0,
        "avg_confidence": int(avg_conf + 0.5) if avg_conf is not None else 0,
    }
