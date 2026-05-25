# SaaS Productization Phase — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google Sheets export, VIP notifications (Telegram + WhatsApp), a lightweight HTML dashboard, tenant suspension, usage quota enforcement, webhook callbacks, and admin config CLI on top of the existing multi-tenant evaluation API.

**Architecture:** Thin integration layer — all post-evaluation side effects live in `tools/integrations.py` and fire via FastAPI `BackgroundTasks` so the evaluation response is never delayed. Schema migrates via `ALTER TABLE ADD COLUMN` at every startup (idempotent, no migration runner). Quota is enforced synchronously before the Claude call so over-quota requests burn zero tokens.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (`sqlite3`), Anthropic SDK, `httpx`, `google-auth`, `google-api-python-client`, `jinja2`, pytest, httpx

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `tools/requirements.txt` | Modify | Add `google-auth`, `google-api-python-client`, `jinja2` |
| `tools/db.py` | Modify | Schema migration, expanded `TenantConfig`, `QuotaExceededError`, `check_quota()`, updated `log_evaluation()`, `get_top_locations()`, `get_recent_evaluations()`, `get_quota_status()`, `get_tenant_raw()`, `update_tenant_fields()`, updated `get_stats_window()` |
| `tools/auth.py` | Modify | Add `is_active` check → HTTP 403 if suspended |
| `tools/claude_evaluator.py` | Modify | `evaluate_lead()` returns `tuple[dict, dict]` with usage |
| `tools/integrations.py` | Create | `fire_integrations()`, `_sheets_append()`, `_notify_vip()`, `_telegram_notify()`, `_whatsapp_notify()`, `_webhook_post()` |
| `tools/templates/dashboard.html` | Create | Jinja2 template — pure HTML/CSS, no frameworks |
| `tools/update_tenant.py` | Create | Admin CLI to update any tenant config field |
| `tools/api_server.py` | Modify | Quota check, `BackgroundTasks`, `GET /dashboard`, extended `/stats` models |
| `tests/test_quota.py` | Create | Quota enforcement unit tests |
| `tests/test_integrations.py` | Create | Integration dispatch unit tests |
| `tests/test_db.py` | Modify | Fix `log_evaluation()` call sites (new signature) |
| `tests/test_stats.py` | Modify | Fix `log_evaluation()` call sites + updated response shape |
| `tests/test_auth.py` | Modify | Add suspended-tenant test + fix `evaluate_lead` mock to return tuple |
| `.env.example` | Modify | Add `GOOGLE_SERVICE_ACCOUNT_JSON` |

---

### Task 1: Update dependencies

**Files:**
- Modify: `tools/requirements.txt`

- [ ] **Step 1: Replace requirements.txt contents**

Write `tools/requirements.txt`:
```
anthropic>=0.50.0
python-dotenv>=1.0.0
fastapi>=0.110.0
uvicorn>=0.29.0
slowapi>=0.5.7
pytest>=8.0.0
httpx>=0.27.0
google-auth>=2.28.0
google-api-python-client>=2.120.0
jinja2>=3.1.0
```

Note: Telegram notifications use `httpx` directly (Telegram Bot API is plain HTTP) — no extra library needed.

- [ ] **Step 2: Install**

```
pip install -r tools/requirements.txt
```

Expected: clean install, no errors.

- [ ] **Step 3: Update .env.example**

Append to `.env.example`:
```
# ── Google Sheets Integration (optional) ──────────────────────────────────────
# Path to service account JSON key file. Leave unset to disable Sheets export.
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service_account.json
```

- [ ] **Step 4: Commit**

```
git add tools/requirements.txt .env.example
git commit -m "chore: add google-auth, google-api-python-client, jinja2 dependencies"
```

---

### Task 2: Extend db.py — schema migration + new functions

**Files:**
- Modify: `tools/db.py`

- [ ] **Step 1: Write the complete updated db.py**

Replace the full contents of `tools/db.py`:

```python
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
```

- [ ] **Step 2: Run existing db tests to confirm they still pass (signature breakage expected)**

```
pytest tests/test_db.py -v 2>&1 | head -40
```

Expected: `test_is_duplicate_true_after_evaluation`, `test_is_duplicate_isolated_per_client`, and `test_log_evaluation_records_entry` fail with `TypeError` (old 4-arg call). All other tests pass. That confirms the migration works and only signature-dependent tests are broken.

- [ ] **Step 3: Commit**

```
git add tools/db.py
git commit -m "feat: extend db schema with lead data, tokens, quota, tenant suspension, notification config"
```

---

### Task 3: Update auth.py — is_active suspension check

**Files:**
- Modify: `tools/auth.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_auth.py` (append to existing file — keep all existing tests):

```python
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
```

- [ ] **Step 2: Run to verify it fails**

```
pytest tests/test_auth.py::test_suspended_tenant_returns_403 -v
```

Expected: FAIL — current `require_tenant` doesn't check `is_active`.

- [ ] **Step 3: Replace tools/auth.py**

```python
from fastapi import Header, HTTPException, status

from tools.db import TenantConfig, get_tenant_by_api_key


def require_tenant(x_api_key: str = Header(...)) -> TenantConfig:
    tenant = get_tenant_by_api_key(x_api_key)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact support.",
        )
    return tenant
```

- [ ] **Step 4: Run auth tests**

```
pytest tests/test_auth.py -v
```

Expected: `test_suspended_tenant_returns_403` passes. The other three existing tests (`test_missing_api_key_returns_422`, `test_invalid_api_key_returns_401`, `test_valid_api_key_passes_auth`) will fail because `evaluate_lead` still returns a dict — that is fixed in Task 8 after Task 4 is done.

- [ ] **Step 5: Commit**

```
git add tools/auth.py tests/test_auth.py
git commit -m "feat: return 403 for suspended tenants in require_tenant()"
```

---

### Task 4: evaluate_lead() returns (result, usage) tuple

**Files:**
- Modify: `tools/claude_evaluator.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_evaluator.py` (append — keep all existing tests):

```python
def test_evaluate_lead_returns_tuple(monkeypatch):
    import anthropic
    from tools.claude_evaluator import evaluate_lead

    class _FakeUsage:
        input_tokens = 120
        output_tokens = 48
        cache_read_input_tokens = 95

    class _FakeContent:
        type = "text"
        text = json.dumps({
            "tier": "VIP",
            "confidence": 88,
            "reasoning": "High budget.",
            "visual_signals": "none",
            "sales_strategy": "Assign to senior closer.",
        })

    class _FakeResponse:
        content = [_FakeContent()]
        usage = _FakeUsage()

    class _FakeClient:
        def __init__(self, **kwargs): pass
        class messages:
            @staticmethod
            def create(**kwargs):
                return _FakeResponse()

    monkeypatch.setattr("tools.claude_evaluator.anthropic.Anthropic", _FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result, usage = evaluate_lead({"lead_name": "Ahmed"})
    assert result["tier"] == "VIP"
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 48
    assert usage["cache_read_tokens"] == 95


def test_evaluate_lead_fallback_returns_tuple(monkeypatch):
    import anthropic
    from tools.claude_evaluator import evaluate_lead, FALLBACK_RESULT

    class _FakeClient:
        def __init__(self, **kwargs): pass
        class messages:
            @staticmethod
            def create(**kwargs):
                raise anthropic.APITimeoutError(request=None)

    monkeypatch.setattr("tools.claude_evaluator.anthropic.Anthropic", _FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result, usage = evaluate_lead({"lead_name": "Ahmed"})
    assert result == FALLBACK_RESULT
    assert usage == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_evaluator.py::test_evaluate_lead_returns_tuple tests/test_evaluator.py::test_evaluate_lead_fallback_returns_tuple -v
```

Expected: FAIL — `evaluate_lead` currently returns a plain dict.

- [ ] **Step 3: Replace tools/claude_evaluator.py**

```python
#!/usr/bin/env python3
"""
Real estate lead evaluation via Claude Sonnet 4.5 Vision.

CLI usage:
    echo '{...}' | python tools/claude_evaluator.py
    python tools/claude_evaluator.py < lead_payload.json

Server usage: imported by tools/api_server.py — call evaluate_lead(lead, tenant).
Returns tuple[dict, dict]: (evaluation_result, token_usage).

Output JSON keys: tier, confidence, reasoning, visual_signals, sales_strategy
Token usage keys: input_tokens, output_tokens, cache_read_tokens

Exit codes (CLI only):
    0  success (including fallback result on API failure)
    1  invalid JSON on stdin or missing ANTHROPIC_API_KEY
"""

import json
import os
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-5"
MAX_OUTPUT_TOKENS = 512
VALID_TIERS = ("VIP", "Medium", "Low")
EMPTY_USAGE: dict = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}

SYSTEM_PROMPT = """\
You are an expert Real Estate Lead Qualifier. Your task is to analyze prospect data — \
including textual signals AND the attached WhatsApp profile picture — and classify the \
lead into one of three tiers: VIP, Medium, or Low, using the classification rules \
provided in the lead profile below.

[Visual Analysis Guidance]
When a profile picture is provided, look for: professional vs casual setting, attire \
formality, visible luxury markers (cars, watches, premium locations), family/lifestyle \
context, or signs of business activity. Do NOT make assumptions based on appearance, \
gender, or ethnicity — focus strictly on contextual wealth and lifestyle signals visible \
in the frame. If the picture is generic, a logo, or absent, ignore this dimension entirely.

[Required Output Format]
Return a strict JSON object with these exact keys and absolutely no additional text, \
markdown fences, or commentary:
{
  "tier": "VIP" | "Medium" | "Low",
  "confidence": <integer 0-100 reflecting certainty of this classification>,
  "reasoning": "A concise 2-sentence explanation of the classification.",
  "visual_signals": "Brief note on what the profile picture contributed (or 'none' if no useful signal).",
  "sales_strategy": "Actionable advice for the human closer on how to approach this lead."
}\
"""

FALLBACK_RESULT: dict = {
    "tier": "Medium",
    "confidence": 0,
    "reasoning": (
        "Unclassified due to API timeout after retry. "
        "Defaulting to Medium to prevent lead loss."
    ),
    "visual_signals": "none",
    "sales_strategy": (
        "Treat as a standard Medium lead — manual review recommended "
        "before assigning to a closer."
    ),
}


def _build_user_content(lead: dict, tenant=None) -> list:
    from tools.db import DEFAULT_TENANT
    cfg = tenant if tenant is not None else DEFAULT_TENANT

    vip_locs = ", ".join(cfg.vip_locations)
    rules = (
        "[Classification Rules — apply these exactly]\n"
        f"- VIP Lead: Target areas ({vip_locs}), "
        f"OR budget >= {cfg.budget_vip_min:,} {cfg.currency}, "
        "OR cash-ready timeline, "
        "OR profile picture shows clear affluence or executive context.\n"
        f"- Medium Lead: Standard residential with payment plan, "
        f"budget {cfg.budget_medium_min:,}–{cfg.budget_vip_min:,} {cfg.currency}, "
        "or 6-month decision timeline.\n"
        f"- Low Lead: Budget < {cfg.budget_medium_min:,} {cfg.currency} with no urgency, "
        "seeking rentals only, VoIP/invalid phone, or unserious engagement.\n"
    )

    lang_note = (
        "\n[Language Instruction]\n"
        "Return all text fields (reasoning, visual_signals, sales_strategy) in Arabic."
        if cfg.output_language == "ar"
        else ""
    )

    content: list = []
    pic_url = (lead.get("wa_profile_picture_url") or "").strip()
    if pic_url:
        content.append({"type": "image", "source": {"type": "url", "url": pic_url}})

    pic_note = (
        "- Profile Picture: [attached — analyze visual context for wealth/lifestyle signals]"
        if pic_url
        else "- Profile Picture: Not available — evaluate on textual signals only."
    )

    text_block = (
        f"{rules}\n"
        "[Lead Profile]\n"
        f"- Lead Name (ManyChat): {lead.get('lead_name') or 'Unknown'}\n"
        f"- WhatsApp Display Name: {lead.get('wa_display_name') or 'Private'}\n"
        f"- WhatsApp Status: {lead.get('wa_status_text') or 'N/A'}\n"
        f"- Preferred Location: {lead.get('location') or 'Not specified'}\n"
        f"- Self-Reported Budget: {lead.get('budget_range') or 'Not specified'}\n"
        f"- Purchase Timeline: {lead.get('timeline') or 'Not specified'}\n"
        f"- Purpose: {lead.get('purpose') or 'Not specified'}\n"
        f"- Phone Carrier/Country: {lead.get('carrier') or 'Unknown'}, "
        f"{lead.get('country_code') or 'Unknown'}\n"
        f"- Phone Validation: {lead.get('phone_valid') or 'unverified'}\n"
        f"{pic_note}"
        f"{lang_note}\n\n"
        "Classify this lead and return ONLY the JSON object."
    )
    content.append({"type": "text", "text": text_block})
    return content


def _parse_claude_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()

    result = json.loads(text)

    required_keys = ("tier", "confidence", "reasoning", "visual_signals", "sales_strategy")
    missing = [k for k in required_keys if k not in result]
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")

    if result["tier"] not in VALID_TIERS:
        raise ValueError(f"Unexpected tier value: {result['tier']!r}")

    conf = result["confidence"]
    if not isinstance(conf, int) or not (0 <= conf <= 100):
        raise ValueError(
            f"Invalid confidence value: {conf!r} — must be an integer 0–100"
        )

    return result


def evaluate_lead(lead: dict, tenant=None) -> tuple[dict, dict]:
    """Evaluate a lead with Claude. Returns (result_dict, usage_dict).

    usage_dict keys: input_tokens, output_tokens, cache_read_tokens.
    On API failure, returns (FALLBACK_RESULT, EMPTY_USAGE).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=api_key, max_retries=1)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": _build_user_content(lead, tenant)}],
        )

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        result = _parse_claude_json(text)
        usage = {
            "input_tokens":      response.usage.input_tokens,
            "output_tokens":     response.usage.output_tokens,
            "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }
        return result, usage

    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"[claude_evaluator] Evaluation failed: {exc}\n")
        return FALLBACK_RESULT, EMPTY_USAGE


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("[claude_evaluator] ANTHROPIC_API_KEY is not set in .env\n")
        sys.exit(1)

    try:
        lead = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[claude_evaluator] Invalid JSON on stdin: {exc}\n")
        sys.exit(1)

    result, _usage = evaluate_lead(lead)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run evaluator tests**

```
pytest tests/test_evaluator.py -v
```

Expected: all 8 tests pass (6 original + 2 new).

- [ ] **Step 5: Commit**

```
git add tools/claude_evaluator.py tests/test_evaluator.py
git commit -m "feat: evaluate_lead() returns (result, usage) tuple with token counts"
```

---

### Task 5: Create tools/integrations.py

**Files:**
- Create: `tools/integrations.py`
- Create: `tests/test_integrations.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_integrations.py`:

```python
import pytest
from unittest.mock import patch, MagicMock, call
from tools.db import TenantConfig
from tools.integrations import fire_integrations, _notify_vip, _webhook_post, _telegram_notify, _whatsapp_notify


def _tenant(**kwargs) -> TenantConfig:
    defaults = dict(
        client_id="agency-01", name="Alpha Realty",
        budget_vip_min=8_000_000, budget_medium_min=3_000_000,
        currency="EGP", vip_locations=["North Coast"],
        output_language="en", monthly_quota=1000, is_active=True,
        sheets_id="", telegram_bot_token="", telegram_chat_id="",
        wa_notify_url="", wa_notify_token="", wa_notify_to="",
        webhook_url="", vip_min_confidence=70,
    )
    defaults.update(kwargs)
    return TenantConfig(**defaults)


VIP = {"tier": "VIP", "confidence": 85, "reasoning": "High budget.", "sales_strategy": "Act now.", "visual_signals": "none"}
LEAD = {"lead_name": "Ahmed Hassan", "phone_number": "+201234567890", "location": "North Coast"}


def test_webhook_fires_when_url_set():
    tenant = _tenant(webhook_url="https://crm.example.com/hook")
    with patch("tools.integrations.httpx.post") as mock_post:
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["event"] == "lead_evaluated"
    assert "event_id" in payload
    assert payload["tenant_id"] == "agency-01"
    assert payload["is_duplicate"] is False
    assert payload["lead"]["name"] == "Ahmed Hassan"
    assert payload["result"]["tier"] == "VIP"


def test_webhook_skips_when_no_url():
    tenant = _tenant(webhook_url="")
    with patch("tools.integrations.httpx.post") as mock_post:
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_not_called()


def test_webhook_failure_does_not_raise():
    tenant = _tenant(webhook_url="https://crm.example.com/hook")
    with patch("tools.integrations.httpx.post", side_effect=Exception("timeout")):
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)  # must not raise


def test_telegram_fires_when_configured():
    tenant = _tenant(telegram_bot_token="7123:AAF", telegram_chat_id="-1001234")
    with patch("tools.integrations.httpx.post") as mock_post:
        _telegram_notify(tenant, "test message")
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    assert "7123:AAF" in url
    assert mock_post.call_args.kwargs["json"]["chat_id"] == "-1001234"
    assert mock_post.call_args.kwargs["json"]["text"] == "test message"


def test_telegram_skips_when_not_configured():
    tenant = _tenant(telegram_bot_token="", telegram_chat_id="")
    with patch("tools.integrations.httpx.post") as mock_post:
        _telegram_notify(tenant, "test message")
    mock_post.assert_not_called()


def test_whatsapp_fires_when_configured():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok123",
        wa_notify_to="+20100000000",
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _whatsapp_notify(tenant, "test message")
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["to"] == "+20100000000"
    assert mock_post.call_args.kwargs["json"]["body"] == "test message"
    assert "tok123" in mock_post.call_args.kwargs["headers"]["Authorization"]


def test_vip_notify_fires_for_vip_high_confidence():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
        vip_min_confidence=70,
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_called_once()


def test_vip_notify_skips_low_confidence():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
        vip_min_confidence=70,
    )
    low_result = {**VIP, "confidence": 50}
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, low_result, is_duplicate=False)
    mock_post.assert_not_called()


def test_vip_notify_skips_non_vip_tier():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    medium_result = {**VIP, "tier": "Medium"}
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, medium_result, is_duplicate=False)
    mock_post.assert_not_called()


def test_vip_notify_skips_duplicate():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, VIP, is_duplicate=True)
    mock_post.assert_not_called()


def test_fire_integrations_one_failure_does_not_block_others():
    tenant = _tenant(
        webhook_url="https://crm.example.com/hook",
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    call_count = 0

    def flaky_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("first call fails")
        return MagicMock(status_code=200)

    with patch("tools.integrations.httpx.post", side_effect=flaky_post):
        fire_integrations(tenant, LEAD, VIP, eval_id=1, is_duplicate=False)

    assert call_count == 2  # webhook failed, WhatsApp still fired
```

- [ ] **Step 2: Run to verify they fail**

```
pytest tests/test_integrations.py -v
```

Expected: `ModuleNotFoundError: No module named 'tools.integrations'`

- [ ] **Step 3: Create tools/integrations.py**

```python
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

from tools.db import TenantConfig

log = logging.getLogger(__name__)

_SHEETS_HEADER = [
    "Timestamp", "Lead Name", "Phone", "Tier", "Confidence",
    "Reasoning", "Source", "Duplicate", "Location",
]

_sheets_svc = None
_sheets_init_attempted = False


def _get_sheets_service():
    global _sheets_svc, _sheets_init_attempted
    if _sheets_init_attempted:
        return _sheets_svc
    _sheets_init_attempted = True
    creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not creds_path:
        log.warning("integrations: GOOGLE_SERVICE_ACCOUNT_JSON not set — Sheets disabled")
        return None
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _sheets_svc
    except Exception as exc:
        log.warning("integrations: failed to init Sheets service", extra={"error": str(exc)})
        return None


def _sheets_append(tenant: TenantConfig, lead: dict, result: dict, is_duplicate: bool) -> None:
    if not tenant.sheets_id:
        return
    svc = _get_sheets_service()
    if svc is None:
        return
    try:
        existing = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=tenant.sheets_id, range="Sheet1!A1:A1")
            .execute()
        )
        if not existing.get("values"):
            svc.spreadsheets().values().append(
                spreadsheetId=tenant.sheets_id,
                range="Sheet1!A1",
                valueInputOption="RAW",
                body={"values": [_SHEETS_HEADER]},
            ).execute()

        row = [
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            lead.get("lead_name") or "",
            lead.get("phone_number") or "",
            result.get("tier", ""),
            result.get("confidence", 0),
            result.get("reasoning", ""),
            lead.get("source", "api"),
            "Yes" if is_duplicate else "No",
            lead.get("location") or "",
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=tenant.sheets_id,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()
    except Exception as exc:
        log.warning(
            "integrations: Sheets append failed",
            extra={"tenant_id": tenant.client_id, "error": str(exc)},
        )


def _build_vip_message(tenant: TenantConfig, lead: dict, result: dict) -> str:
    return (
        f"\U0001f514 VIP Lead — {tenant.name}\n"
        f"Name: {lead.get('lead_name') or 'Unknown'}\n"
        f"Phone: {lead.get('phone_number') or 'N/A'}\n"
        f"Confidence: {result.get('confidence', 0)}%\n"
        f"Location: {lead.get('location') or 'N/A'}\n"
        f"Reasoning: {result.get('reasoning', '')}\n"
        f"Action: {result.get('sales_strategy', '')}"
    )


def _telegram_notify(tenant: TenantConfig, message: str) -> None:
    if not tenant.telegram_bot_token or not tenant.telegram_chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{tenant.telegram_bot_token}/sendMessage",
            json={"chat_id": tenant.telegram_chat_id, "text": message},
            timeout=10,
        )
    except Exception as exc:
        log.warning(
            "integrations: Telegram notify failed",
            extra={"tenant_id": tenant.client_id, "error": str(exc)},
        )


def _whatsapp_notify(tenant: TenantConfig, message: str) -> None:
    if not tenant.wa_notify_url or not tenant.wa_notify_token or not tenant.wa_notify_to:
        return
    try:
        httpx.post(
            tenant.wa_notify_url,
            json={"to": tenant.wa_notify_to, "body": message},
            headers={"Authorization": f"Bearer {tenant.wa_notify_token}"},
            timeout=10,
        )
    except Exception as exc:
        log.warning(
            "integrations: WhatsApp notify failed",
            extra={"tenant_id": tenant.client_id, "error": str(exc)},
        )


def _notify_vip(tenant: TenantConfig, lead: dict, result: dict, is_duplicate: bool) -> None:
    if is_duplicate:
        return
    if result.get("tier") != "VIP":
        return
    if result.get("confidence", 0) < tenant.vip_min_confidence:
        return
    message = _build_vip_message(tenant, lead, result)
    _telegram_notify(tenant, message)
    _whatsapp_notify(tenant, message)


def _webhook_post(tenant: TenantConfig, lead: dict, result: dict, is_duplicate: bool) -> None:
    if not tenant.webhook_url:
        return
    try:
        httpx.post(
            tenant.webhook_url,
            json={
                "event": "lead_evaluated",
                "event_id": str(uuid.uuid4()),
                "tenant_id": tenant.client_id,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "lead": {
                    "name":     lead.get("lead_name") or "",
                    "phone":    lead.get("phone_number") or "",
                    "location": lead.get("location") or "",
                },
                "result": {
                    "tier":           result.get("tier", ""),
                    "confidence":     result.get("confidence", 0),
                    "reasoning":      result.get("reasoning", ""),
                    "sales_strategy": result.get("sales_strategy", ""),
                },
                "is_duplicate": is_duplicate,
            },
            timeout=10,
        )
    except Exception as exc:
        log.warning(
            "integrations: webhook post failed",
            extra={"tenant_id": tenant.client_id, "error": str(exc)},
        )


def fire_integrations(
    tenant: TenantConfig,
    lead: dict,
    result: dict,
    eval_id: int,
    is_duplicate: bool,
) -> None:
    _sheets_append(tenant, lead, result, is_duplicate)
    _notify_vip(tenant, lead, result, is_duplicate)
    _webhook_post(tenant, lead, result, is_duplicate)
```

- [ ] **Step 4: Run integration tests**

```
pytest tests/test_integrations.py -v
```

Expected: all 13 tests pass.

- [ ] **Step 5: Commit**

```
git add tools/integrations.py tests/test_integrations.py
git commit -m "feat: add post-evaluation integration layer (Sheets, Telegram, WhatsApp, webhook)"
```

---

### Task 6: Create dashboard template

**Files:**
- Create: `tools/templates/dashboard.html`

- [ ] **Step 1: Create the templates directory and write dashboard.html**

```
mkdir tools\templates
```

Create `tools/templates/dashboard.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ tenant.name }} — Lead Dashboard</title>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d27; --border: #2d3148;
  --text: #e2e8f0; --muted: #94a3b8;
  --vip: #f59e0b; --medium: #3b82f6; --low: #6b7280;
  --danger: #ef4444;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, sans-serif; font-size: 14px; line-height: 1.5; }
.header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
.header h1 { font-size: 17px; font-weight: 600; }
.header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
.nav { display: flex; gap: 6px; }
.nav a { padding: 5px 12px; border-radius: 5px; text-decoration: none; color: var(--muted); font-size: 12px; border: 1px solid var(--border); }
.nav a.active { background: var(--medium); color: #fff; border-color: var(--medium); }
.wrap { padding: 20px 24px; max-width: 1080px; margin: 0 auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.card .lbl { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }
.card .val { font-size: 26px; font-weight: 700; }
.card .val.gold { color: var(--vip); }
.card .val.red  { color: var(--danger); }
.qbar-bg { background: var(--bg); border-radius: 3px; height: 5px; margin-top: 6px; overflow: hidden; }
.qbar    { height: 100%; border-radius: 3px; }
.section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px 20px; margin-bottom: 20px; }
.section h2 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 14px; }
.loc-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.loc-name { width: 130px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.loc-bg   { flex: 1; background: var(--bg); border-radius: 3px; height: 14px; overflow: hidden; }
.loc-fill { background: var(--medium); height: 100%; border-radius: 3px; }
.loc-cnt  { width: 30px; text-align: right; color: var(--muted); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; padding: 7px 10px; border-bottom: 1px solid var(--border); }
td { padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
.badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.VIP    { background: rgba(245,158,11,.15); color: var(--vip); }
.Medium { background: rgba(59,130,246,.15);  color: var(--medium); }
.Low    { background: rgba(107,114,128,.15); color: var(--low); }
.dup    { font-size: 10px; color: var(--muted); margin-left: 4px; }
.mono   { font-family: monospace; color: var(--muted); }
.empty  { color: var(--muted); text-align: center; padding: 28px; }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>{{ tenant.name }}</h1>
    <div class="sub">Lead Dashboard</div>
  </div>
  <nav class="nav">
    <a href="?window=24h" class="{{ 'active' if window == '24h' else '' }}">24h</a>
    <a href="?window=7d"  class="{{ 'active' if window == '7d'  else '' }}">7d</a>
    <a href="?window=30d" class="{{ 'active' if window == '30d' else '' }}">30d</a>
  </nav>
</div>
<div class="wrap">
  <div class="cards">
    <div class="card">
      <div class="lbl">Total Leads</div>
      <div class="val">{{ stats.total }}</div>
    </div>
    <div class="card">
      <div class="lbl">VIP Leads</div>
      <div class="val gold">{{ stats.vip }}</div>
    </div>
    <div class="card">
      <div class="lbl">Duplicates</div>
      <div class="val">{{ stats.duplicates }}</div>
    </div>
    <div class="card">
      <div class="lbl">Avg Confidence</div>
      <div class="val">{{ stats.avg_confidence }}%</div>
    </div>
    <div class="card">
      <div class="lbl">Quota Used</div>
      <div class="val {{ 'red' if quota.remaining == 0 else '' }}">{{ quota.used }}/{{ quota.limit }}</div>
      {% set pct = [(quota.used * 100 // quota.limit if quota.limit else 0), 100] | min %}
      <div class="qbar-bg">
        <div class="qbar" style="width:{{ pct }}%;background:{{ '#ef4444' if quota.remaining == 0 else '#3b82f6' }};"></div>
      </div>
    </div>
  </div>

  {% if locations %}
  <div class="section">
    <h2>Top Locations</h2>
    {% for loc in locations %}
    <div class="loc-row">
      <div class="loc-name">{{ loc.location }}</div>
      <div class="loc-bg">
        <div class="loc-fill" style="width:{{ (loc.count * 100 // max_loc_count) if max_loc_count else 0 }}%;"></div>
      </div>
      <div class="loc-cnt">{{ loc.count }}</div>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <div class="section">
    <h2>Recent Evaluations</h2>
    {% if recent %}
    <table>
      <thead>
        <tr>
          <th>Lead</th><th>Phone</th><th>Tier</th><th>Conf</th><th>Location</th><th>Time</th>
        </tr>
      </thead>
      <tbody>
        {% for row in recent %}
        <tr>
          <td>{{ row.lead_name or 'Unknown' }}</td>
          <td class="mono">{{ row.phone_masked }}</td>
          <td>
            <span class="badge {{ row.tier }}">{{ row.tier }}</span>
            {% if row.is_duplicate %}<span class="dup">dup</span>{% endif %}
          </td>
          <td>{{ row.confidence }}%</td>
          <td>{{ row.location or '—' }}</td>
          <td style="color:var(--muted)">{{ row.evaluated_at[:16] | replace('T', ' ') }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="empty">No evaluations yet in this window.</p>
    {% endif %}
  </div>
</div>
</body>
</html>
```

- [ ] **Step 2: Commit**

```
git add tools/templates/dashboard.html
git commit -m "feat: add lightweight HTML dashboard template (pure HTML/CSS, no frameworks)"
```

---

### Task 7: Create update_tenant.py admin CLI

**Files:**
- Create: `tools/update_tenant.py`

- [ ] **Step 1: Create tools/update_tenant.py**

```python
#!/usr/bin/env python3
"""
Admin CLI to update tenant configuration fields.

Usage (update fields):
    python tools/update_tenant.py agency-01 --budget-vip-min 10000000
    python tools/update_tenant.py agency-01 --sheets-id 1BxiMVs...
    python tools/update_tenant.py agency-01 --telegram-bot-token 7123:AAF --telegram-chat-id -1001234
    python tools/update_tenant.py agency-01 --wa-notify-url https://... --wa-notify-token tok --wa-notify-to +201...
    python tools/update_tenant.py agency-01 --webhook-url https://crm.example.com/hooks/leads
    python tools/update_tenant.py agency-01 --monthly-quota 2000
    python tools/update_tenant.py agency-01 --suspend
    python tools/update_tenant.py agency-01 --activate

Usage (inspect — no flags):
    python tools/update_tenant.py agency-01
"""

import argparse
import sys

from tools.db import get_tenant_raw, init_db, update_tenant_fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Update tenant configuration")
    parser.add_argument("client_id", help="Tenant client_id (e.g. agency-01)")

    parser.add_argument("--budget-vip-min",    type=int)
    parser.add_argument("--budget-medium-min", type=int)
    parser.add_argument("--currency")
    parser.add_argument("--vip-locations",     help="Comma-separated list of VIP location names")
    parser.add_argument("--output-language",   choices=["en", "ar"])
    parser.add_argument("--monthly-quota",     type=int)
    parser.add_argument("--sheets-id")
    parser.add_argument("--telegram-bot-token")
    parser.add_argument("--telegram-chat-id")
    parser.add_argument("--wa-notify-url")
    parser.add_argument("--wa-notify-token")
    parser.add_argument("--wa-notify-to")
    parser.add_argument("--webhook-url")
    parser.add_argument("--vip-min-confidence", type=int)

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--suspend",  action="store_true", help="Suspend tenant (blocks all API access)")
    group.add_argument("--activate", action="store_true", help="Re-activate a suspended tenant")

    args = parser.parse_args()
    init_db()

    current = get_tenant_raw(args.client_id)
    if current is None:
        print(f"Error: tenant '{args.client_id}' not found.", file=sys.stderr)
        sys.exit(1)

    # Build updates dict from provided flags
    updates: dict = {}
    flag_map = [
        ("budget_vip_min",    args.budget_vip_min),
        ("budget_medium_min", args.budget_medium_min),
        ("currency",          args.currency),
        ("output_language",   args.output_language),
        ("monthly_quota",     args.monthly_quota),
        ("sheets_id",         args.sheets_id),
        ("telegram_bot_token", args.telegram_bot_token),
        ("telegram_chat_id",  args.telegram_chat_id),
        ("wa_notify_url",     args.wa_notify_url),
        ("wa_notify_token",   args.wa_notify_token),
        ("wa_notify_to",      args.wa_notify_to),
        ("webhook_url",       args.webhook_url),
        ("vip_min_confidence", args.vip_min_confidence),
    ]
    for col, val in flag_map:
        if val is not None:
            updates[col] = val

    if args.vip_locations is not None:
        updates["vip_locations"] = args.vip_locations  # stored as comma-separated string

    if args.suspend:
        updates["is_active"] = 0
    if args.activate:
        updates["is_active"] = 1

    # Inspection mode — no flags provided
    if not updates:
        print(f"Config for {args.client_id}:")
        skip = {"api_key_hash"}
        for key, value in current.items():
            if key not in skip:
                print(f"  {key}: {value}")
        return

    # Apply and report changes
    update_tenant_fields(args.client_id, updates)
    for col, new_val in updates.items():
        old_val = current.get(col, "—")
        if col == "is_active":
            print(f"  {args.client_id} {'suspended' if new_val == 0 else 'activated'}.")
        else:
            print(f"  {col}: {old_val!r} → {new_val!r}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the CLI (requires a seeded DB)**

If you have a seeded tenant in `data/leads.db`, run:
```
python tools/update_tenant.py <your-client-id>
```
Expected: prints current config without error.

If no DB exists yet, skip this step — the CLI will be exercised via integration during full test run.

- [ ] **Step 3: Commit**

```
git add tools/update_tenant.py
git commit -m "feat: add update_tenant admin CLI for config changes and tenant suspension"
```

---

### Task 8: Wire api_server.py

**Files:**
- Modify: `tools/api_server.py`

- [ ] **Step 1: Replace tools/api_server.py**

```python
#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

POST /evaluate  — requires X-API-Key header, rate-limited 60 req/min per key
GET  /stats     — requires X-API-Key header
GET  /dashboard — requires X-API-Key header, returns HTML
GET  /health    — liveness check, no auth required

Start:
    uvicorn tools.api_server:app --host 0.0.0.0 --port 8000
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from tools.auth import require_tenant
from tools.claude_evaluator import evaluate_lead
from tools.db import (
    QuotaExceededError,
    TenantConfig,
    check_quota,
    get_quota_status,
    get_recent_evaluations,
    get_stats_window,
    get_top_locations,
    hash_phone,
    init_db,
    is_duplicate,
    log_evaluation,
)
from tools.integrations import fire_integrations
from tools.logging_config import configure_logging

load_dotenv()

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _tenant_key(request: Request) -> str:
    return request.headers.get("X-API-Key", "anonymous")


limiter = Limiter(key_func=_tenant_key)


class _RequestLogger(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - t0) * 1000)
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": ms,
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY is not set — refusing to start")
    init_db()
    log.info("startup complete")
    yield


app = FastAPI(title="Lead Evaluator", version="3.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(_RequestLogger)


# ── Pydantic models ────────────────────────────────────────────────────────────

class LeadPayload(BaseModel):
    lead_name: str = ""
    wa_display_name: str = "Private"
    wa_status_text: str = ""
    location: str = ""
    budget_range: str = ""
    timeline: str = ""
    purpose: str = ""
    carrier: str = ""
    country_code: str = ""
    phone_valid: str = "unverified"
    phone_number: str = ""
    wa_profile_picture_url: str = ""


class EvaluationResult(BaseModel):
    tier: str
    confidence: int
    reasoning: str
    visual_signals: str
    sales_strategy: str
    is_duplicate: bool = False


class TokenStats(BaseModel):
    input: int
    output: int
    cache_read: int
    estimated_usd: float


class QuotaStats(BaseModel):
    used: int
    limit: int
    remaining: int


class WindowStats(BaseModel):
    total: int
    vip: int
    medium: int
    low: int
    duplicates: int
    avg_confidence: int
    tokens: TokenStats


class StatsResponse(BaseModel):
    client_id: str
    quota: QuotaStats
    windows: dict[str, WindowStats]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/stats", response_model=StatsResponse)
def stats(tenant: TenantConfig = Depends(require_tenant)) -> StatsResponse:
    try:
        quota_data = get_quota_status(tenant.client_id, tenant.monthly_quota)
        windows = {}
        for label, hours in [("last_24h", 24), ("last_7d", 168), ("last_30d", 720)]:
            w = get_stats_window(tenant.client_id, hours)
            windows[label] = WindowStats(
                total=w["total"],
                vip=w["vip"],
                medium=w["medium"],
                low=w["low"],
                duplicates=w["duplicates"],
                avg_confidence=w["avg_confidence"],
                tokens=TokenStats(
                    input=w["input_tokens"],
                    output=w["output_tokens"],
                    cache_read=w["cache_read_tokens"],
                    estimated_usd=w["estimated_usd"],
                ),
            )
        return StatsResponse(
            client_id=tenant.client_id,
            quota=QuotaStats(**quota_data),
            windows=windows,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="stats unavailable") from exc


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    window: str = "30d",
    tenant: TenantConfig = Depends(require_tenant),
) -> HTMLResponse:
    hours_map = {"24h": 24, "7d": 168, "30d": 720}
    hours = hours_map.get(window, 720)
    stats_data = get_stats_window(tenant.client_id, hours)
    quota = get_quota_status(tenant.client_id, tenant.monthly_quota)
    locations = get_top_locations(tenant.client_id, hours)
    recent = get_recent_evaluations(tenant.client_id, limit=20)
    max_loc_count = max((loc["count"] for loc in locations), default=1)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tenant": tenant,
            "window": window,
            "stats": stats_data,
            "quota": quota,
            "locations": locations,
            "max_loc_count": max_loc_count,
            "recent": recent,
        },
    )


@app.post("/evaluate", response_model=EvaluationResult)
@limiter.limit("60/minute")
def evaluate(
    request: Request,
    payload: LeadPayload,
    background_tasks: BackgroundTasks,
    tenant: TenantConfig = Depends(require_tenant),
) -> EvaluationResult:
    request_id = getattr(request.state, "request_id", "-")

    try:
        check_quota(tenant.client_id, tenant.monthly_quota)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly quota of {exc.limit} evaluations reached. Contact support to upgrade.",
        )

    phone_hash = hash_phone(payload.phone_number) if payload.phone_number else ""

    if phone_hash and is_duplicate(tenant.client_id, phone_hash):
        dup_result = {
            "tier": "Medium",
            "confidence": 0,
            "reasoning": "Duplicate submission — this phone was evaluated within the last 24 hours.",
            "visual_signals": "none",
            "sales_strategy": "Check the original evaluation record for this lead.",
        }
        eval_id = log_evaluation(
            tenant.client_id, phone_hash, payload.phone_number,
            payload.lead_name, payload.location,
            "Medium", 0,
            "Duplicate submission — this phone was evaluated within the last 24 hours.",
            0, 0, 0, is_dup=True,
        )
        background_tasks.add_task(
            fire_integrations, tenant, payload.model_dump(), dup_result, eval_id, True
        )
        log.info(
            "evaluation",
            extra={
                "request_id": request_id, "tenant_id": tenant.client_id,
                "tier": "Medium", "confidence": 0, "is_duplicate": True,
            },
        )
        return EvaluationResult(**dup_result, is_duplicate=True)

    try:
        result, usage = evaluate_lead(payload.model_dump(), tenant)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    eval_id = None
    if phone_hash:
        eval_id = log_evaluation(
            tenant.client_id, phone_hash, payload.phone_number,
            payload.lead_name, payload.location,
            result["tier"], result.get("confidence", 0),
            result.get("reasoning", ""),
            usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"],
        )

    background_tasks.add_task(
        fire_integrations, tenant, payload.model_dump(), result, eval_id, False
    )

    log.info(
        "evaluation",
        extra={
            "request_id": request_id, "tenant_id": tenant.client_id,
            "tier": result["tier"], "confidence": result.get("confidence", 0),
            "is_duplicate": False,
        },
    )
    return EvaluationResult(**result)
```

- [ ] **Step 2: Run the full test suite — expect only pre-existing signature failures**

```
pytest tests/ -v 2>&1 | tail -30
```

Expected failures at this point:
- `tests/test_db.py` — 3 tests calling old `log_evaluation` signature
- `tests/test_stats.py` — 4 tests calling old `log_evaluation` + 2 checking old response shape
- `tests/test_auth.py::test_valid_api_key_passes_auth` — mock still returns dict not tuple (fixed in Task 9)

All other tests should pass.

- [ ] **Step 3: Commit**

```
git add tools/api_server.py
git commit -m "feat: wire quota enforcement, BackgroundTasks integrations, dashboard, extended stats"
```

---

### Task 9: Fix existing tests + add quota tests

**Files:**
- Create: `tests/test_quota.py`
- Modify: `tests/test_db.py`
- Modify: `tests/test_stats.py`
- Modify: `tests/test_auth.py`

- [ ] **Step 1: Create tests/test_quota.py**

```python
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

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    init_db()
    create_tenant("agency-q", "quota-key", "Quota Agency")
    # Insert 1000 evaluations directly to exceed default quota
    import sqlite3
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
```

- [ ] **Step 2: Run quota tests**

```
pytest tests/test_quota.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 3: Fix test_db.py — update log_evaluation call sites**

In `tests/test_db.py`, replace the three old `log_evaluation(...)` calls:

Old (line 60):
```python
    log_evaluation("agency-01", phone_hash, "VIP", 92)
```
New:
```python
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 92, "reasoning", 100, 50, 80)
```

Old (line 83):
```python
    log_evaluation("agency-01", phone_hash, "VIP", 88)
```
New:
```python
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 88, "reasoning", 100, 50, 80)
```

Old (line 91):
```python
    log_evaluation("agency-01", phone_hash, "VIP", 90)
```
New:
```python
    log_evaluation("agency-01", phone_hash, "+201234567890", "Test Lead", "North Coast", "VIP", 90, "reasoning", 100, 50, 80)
```

- [ ] **Step 4: Run test_db.py**

```
pytest tests/test_db.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Fix test_stats.py — update log_evaluation calls + response shape**

In `tests/test_stats.py`, add a helper at the top (after imports):

```python
def _log(client_id, phone, tier, confidence, is_dup=False):
    log_evaluation(
        client_id, hash_phone(phone), phone, "Test Lead", "North Coast",
        tier, confidence, "reasoning", 100, 50, 80, is_dup=is_dup,
    )
```

Replace every `log_evaluation(...)` call in the file with `_log(...)`:

| Old call | New call |
|---|---|
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 90)` | `_log("agency-01", "+1", "VIP", 90)` |
| `log_evaluation("agency-01", hash_phone("+2"), "Medium", 70)` | `_log("agency-01", "+2", "Medium", 70)` |
| `log_evaluation("agency-01", hash_phone("+3"), "Low", 40)` | `_log("agency-01", "+3", "Low", 40)` |
| `log_evaluation("agency-01", hash_phone("+4"), "VIP", 0, is_dup=True)` | `_log("agency-01", "+4", "VIP", 0, is_dup=True)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 88)` | `_log("agency-01", "+1", "VIP", 88)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)` | `_log("agency-01", "+1", "VIP", 80)` |
| `log_evaluation("agency-01", hash_phone("+2"), "Medium", 90)` | `_log("agency-01", "+2", "Medium", 90)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)` | `_log("agency-01", "+1", "VIP", 80)` |
| `log_evaluation("agency-01", hash_phone("+2"), "Medium", 0, is_dup=True)` | `_log("agency-01", "+2", "Medium", 0, is_dup=True)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 80)` | `_log("agency-01", "+1", "VIP", 80)` |
| `log_evaluation("agency-01", hash_phone("+2"), "Medium", 81)` | `_log("agency-01", "+2", "Medium", 81)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 0, is_dup=True)` | `_log("agency-01", "+1", "VIP", 0, is_dup=True)` |
| `log_evaluation("agency-01", hash_phone("+1"), "VIP", 90)` | `_log("agency-01", "+1", "VIP", 90)` |

Also fix the two tests that assert exact window dict shape:

**`test_empty_window_returns_zeroed_counts`** — replace the assertion:
```python
    assert stats["total"] == 0
    assert stats["vip"] == 0
    assert stats["medium"] == 0
    assert stats["low"] == 0
    assert stats["duplicates"] == 0
    assert stats["avg_confidence"] == 0
    assert stats["input_tokens"] == 0
    assert stats["output_tokens"] == 0
    assert stats["estimated_usd"] == 0.0
```

**`test_stats_empty_db_returns_zeroed_counts`** — replace the assertion:
```python
    window = resp.json()["windows"]["last_24h"]
    assert window["total"] == 0
    assert window["vip"] == 0
    assert window["avg_confidence"] == 0
    assert window["tokens"]["input"] == 0
    assert window["tokens"]["estimated_usd"] == 0.0
```

Also add a check for the new `quota` top-level field in `test_stats_returns_200_with_all_windows`:
```python
    assert "quota" in data
    assert data["quota"]["limit"] == 1000
```

- [ ] **Step 6: Run test_stats.py**

```
pytest tests/test_stats.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Fix test_auth.py — update evaluate_lead mock to return tuple**

In `tests/test_auth.py`, find `test_valid_api_key_passes_auth` and update the monkeypatch:

```python
def test_valid_api_key_passes_auth(client, monkeypatch):
    monkeypatch.setattr(
        "tools.api_server.evaluate_lead",
        lambda lead, tenant: (
            {
                "tier": "Medium",
                "confidence": 70,
                "reasoning": "Standard profile.",
                "visual_signals": "none",
                "sales_strategy": "Add to standard pipeline.",
            },
            {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 80},
        ),
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
```

- [ ] **Step 8: Run the full test suite**

```
pytest tests/ -v
```

Expected: all tests pass. Zero failures.

- [ ] **Step 9: Commit**

```
git add tests/test_quota.py tests/test_db.py tests/test_stats.py tests/test_auth.py
git commit -m "test: add quota tests, fix log_evaluation signatures, update stats/auth tests for new shapes"
```

---

### Task 10: Final verification

- [ ] **Step 1: Run the full test suite one more time from a clean state**

```
pytest tests/ -v --tb=short
```

Expected: all tests pass, zero failures, zero errors.

- [ ] **Step 2: Verify the server starts cleanly**

```
uvicorn tools.api_server:app --host 0.0.0.0 --port 8000
```

Expected log output:
```
startup complete
```
No `RuntimeError` (ANTHROPIC_API_KEY must be set). Hit `GET /health` → `{"status": "ok"}`.

- [ ] **Step 3: Final commit**

```
git add -A
git status
git commit -m "feat: complete SaaS productization phase — quota, integrations, dashboard, suspension, webhooks"
```

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|---|---|
| Schema migration — evaluations (8 columns) | Task 2 (`_add_column_if_missing`) |
| Schema migration — tenants (10 columns) | Task 2 (`_add_column_if_missing`) |
| `is_active` suspension + 403 | Tasks 2, 3 |
| Quota auto-block before Claude call | Tasks 2, 8 |
| Token tracking in evaluations | Tasks 2, 4, 8 |
| `evaluate_lead()` returns tuple | Task 4 |
| Google Sheets append with header | Task 5 (`_sheets_append`) |
| Telegram notification via HTTP | Task 5 (`_telegram_notify`) |
| WhatsApp notification via HTTP | Task 5 (`_whatsapp_notify`) |
| VIP only, above confidence threshold, not duplicate | Task 5 (`_notify_vip`) |
| Webhook with `event_id` | Task 5 (`_webhook_post`) |
| Phone never in logs | Tasks 5, 8 (log.info calls only include tenant_id/tier/confidence) |
| Reasoning capped at 500 chars | Task 2 (`log_evaluation`: `reasoning[:500]`) |
| Explicit timeouts on all outbound calls | Task 5 (all `httpx.post` have `timeout=10`) |
| Integration failures isolated | Task 5 (each sub-function wrapped in try/except) |
| BackgroundTasks — no latency impact | Task 8 (`background_tasks.add_task`) |
| Dashboard HTML — pure HTML/CSS | Task 6 |
| Dashboard phone masked `****1234` | Task 2 (`get_recent_evaluations` SQL) |
| Dashboard window toggle (24h/7d/30d) | Task 6 (plain `<a href="?window=">` links) |
| `/stats` extended with quota + tokens | Task 8 (`StatsResponse` + new models) |
| `update_tenant.py` admin CLI | Task 7 |
| `--suspend` / `--activate` flags | Task 7 |
| Inspection mode (no flags → prints config) | Task 7 |
| `requirements.txt` updated | Task 1 |
| `.env.example` updated | Task 1 |
| All existing tests updated + pass | Task 9 |
