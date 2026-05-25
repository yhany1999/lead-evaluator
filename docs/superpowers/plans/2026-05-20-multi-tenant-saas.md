# Multi-Tenant SaaS Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the single-tenant lead evaluation API into a multi-tenant production SaaS with API key authentication, per-client configuration, usage logging, phone deduplication, and confidence scoring.

**Architecture:** A SQLite persistence layer (`tools/db.py`) stores tenants (hashed API keys + per-client config) and evaluation logs. A FastAPI dependency (`tools/auth.py`) validates `X-API-Key` headers and returns the requesting tenant's config. Per-client thresholds are injected into the **user message** (not the system prompt) so prompt caching remains effective across all tenants. The evaluator output gains a `confidence` field. `app.py` is deleted as a conflicting artifact.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (stdlib `sqlite3`), Anthropic SDK, pytest, httpx

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `tools/db.py` | Create | Schema init, `TenantConfig` dataclass, tenant CRUD, evaluation logging, dedup TTL check, phone/key hashing |
| `tools/auth.py` | Create | FastAPI `Depends` that validates `X-API-Key` header and returns `TenantConfig` |
| `tools/seed_tenant.py` | Create | One-shot CLI to provision a new tenant and print its generated API key |
| `tools/claude_evaluator.py` | Modify | Add `confidence` to output schema; accept `TenantConfig` for per-client threshold injection in user message |
| `tools/api_server.py` | Modify | Wire auth dependency, dedup check, evaluation logging; add `is_duplicate` to response |
| `tools/requirements.txt` | Modify | Add `fastapi`, `uvicorn`, `pytest`, `httpx` |
| `tests/__init__.py` | Create | Empty — marks `tests/` as a package |
| `tests/conftest.py` | Create | `tmp_db` pytest fixture that patches `DB_PATH` to a temp location |
| `tests/test_db.py` | Create | Unit tests for persistence layer |
| `tests/test_auth.py` | Create | Integration tests for auth via FastAPI `TestClient` |
| `tests/test_evaluator.py` | Create | Unit tests for `_parse_claude_json` confidence validation and `FALLBACK_RESULT` |
| `app.py` | Delete | Redundant — uses wrong model (`opus-4-7`), no caching, inconsistent tier labels |
| `.gitignore` | Modify | Add `data/` so the SQLite DB is never committed |

---

### Task 1: Persistence layer (requirements + test infra + db.py)

**Files:**
- Modify: `tools/requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tools/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Update requirements.txt**

Replace the full contents of `tools/requirements.txt`:
```
anthropic>=0.50.0
python-dotenv>=1.0.0
fastapi>=0.110.0
uvicorn>=0.29.0
pytest>=8.0.0
httpx>=0.27.0
```

- [ ] **Step 2: Install dependencies**

```
pip install -r tools/requirements.txt
```

Expected: packages install cleanly.

- [ ] **Step 3: Create test package init**

Create `tests/__init__.py` as an empty file.

- [ ] **Step 4: Create conftest.py**

Create `tests/conftest.py`:
```python
import pytest
from tools import db as db_module


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()
    yield
```

- [ ] **Step 5: Write failing tests for db.py**

Create `tests/test_db.py`:
```python
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
    log_evaluation("agency-01", phone_hash, "VIP", 92)
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
    log_evaluation("agency-01", phone_hash, "VIP", 88)
    # Same phone, different client — must NOT be a duplicate
    assert is_duplicate("agency-02", phone_hash) is False


def test_log_evaluation_records_entry(tmp_db):
    create_tenant("agency-01", "key", "Alpha")
    phone_hash = hash_phone("+201234567890")
    log_evaluation("agency-01", phone_hash, "VIP", 90)
    conn = sqlite3.connect(str(db_module.DB_PATH))
    row = conn.execute(
        "SELECT tier, confidence FROM evaluations WHERE client_id = ?", ("agency-01",)
    ).fetchone()
    conn.close()
    assert row[0] == "VIP"
    assert row[1] == 90
```

- [ ] **Step 6: Run tests — verify they all fail**

```
pytest tests/test_db.py -v
```
Expected: all fail with `ModuleNotFoundError: No module named 'tools.db'`

- [ ] **Step 7: Create tools/db.py**

Create `tools/db.py`:
```python
import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    cutoff = (datetime.utcnow() - timedelta(hours=DEDUP_TTL_HOURS)).isoformat(sep=" ")
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
```

- [ ] **Step 8: Run tests — verify they all pass**

```
pytest tests/test_db.py -v
```
Expected: all 9 tests pass.

- [ ] **Step 9: Add data/ to .gitignore**

Append `data/` as a new line to `.gitignore`.

- [ ] **Step 10: Commit**

```
git add tools/db.py tools/requirements.txt tests/__init__.py tests/conftest.py tests/test_db.py .gitignore
git commit -m "feat: add SQLite persistence layer with tenant config and evaluation logging"
```

---

### Task 2: API key authentication dependency + wire into server

**Files:**
- Create: `tools/auth.py`
- Create: `tests/test_auth.py`
- Modify: `tools/api_server.py`

- [ ] **Step 1: Write failing auth tests**

Create `tests/test_auth.py`:
```python
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
```

- [ ] **Step 2: Run tests — verify they fail**

```
pytest tests/test_auth.py -v
```
Expected: `ImportError` — `tools.auth` and updated `api_server` don't exist yet.

- [ ] **Step 3: Create tools/auth.py**

Create `tools/auth.py`:
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
    return tenant
```

- [ ] **Step 4: Replace tools/api_server.py**

Replace the full contents of `tools/api_server.py`:
```python
#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

POST /evaluate  — requires X-API-Key header (tenant API key)
GET  /health    — liveness check

Start:
    uvicorn tools.api_server:app --host 0.0.0.0 --port 8000

n8n HTTP Request node config:
    Method  : POST
    URL     : http://<your-host>:8000/evaluate
    Headers : X-API-Key: <tenant api key>
    Body    : JSON (enriched lead object)
"""
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from tools.auth import require_tenant
from tools.claude_evaluator import evaluate_lead
from tools.db import TenantConfig, hash_phone, init_db, is_duplicate, log_evaluation

app = FastAPI(title="Lead Evaluator", version="2.0.0")


@app.on_event("startup")
def startup() -> None:
    init_db()


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluationResult)
def evaluate(
    payload: LeadPayload,
    tenant: TenantConfig = Depends(require_tenant),
) -> EvaluationResult:
    phone_hash = hash_phone(payload.phone_number) if payload.phone_number else ""

    if phone_hash and is_duplicate(tenant.client_id, phone_hash):
        log_evaluation(tenant.client_id, phone_hash, "Medium", 0, is_dup=True)
        return EvaluationResult(
            tier="Medium",
            confidence=0,
            reasoning="Duplicate submission — this phone was evaluated within the last 24 hours.",
            visual_signals="none",
            sales_strategy="Check the original evaluation record for this lead.",
            is_duplicate=True,
        )

    try:
        result = evaluate_lead(payload.model_dump(), tenant)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if phone_hash:
        log_evaluation(
            tenant.client_id, phone_hash, result["tier"], result.get("confidence", 0)
        )

    return EvaluationResult(**result)
```

- [ ] **Step 5: Run tests — verify they pass**

```
pytest tests/test_auth.py -v
```
Expected: all 3 tests pass.

- [ ] **Step 6: Commit**

```
git add tools/auth.py tools/api_server.py tests/test_auth.py
git commit -m "feat: add API key auth with tenant isolation, dedup, and usage logging"
```

---

### Task 3: Confidence scoring + tenant config injection in evaluator

**Files:**
- Modify: `tools/claude_evaluator.py`
- Create: `tests/test_evaluator.py`

- [ ] **Step 1: Write failing evaluator tests**

Create `tests/test_evaluator.py`:
```python
import json
import pytest
from tools.claude_evaluator import FALLBACK_RESULT, _parse_claude_json


def _valid_json(**overrides) -> str:
    base = {
        "tier": "VIP",
        "confidence": 87,
        "reasoning": "High budget and VIP location.",
        "visual_signals": "Professional attire visible.",
        "sales_strategy": "Assign to senior closer immediately.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_parse_returns_confidence_field():
    result = _parse_claude_json(_valid_json())
    assert "confidence" in result
    assert result["confidence"] == 87


def test_parse_rejects_non_integer_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence="high"))


def test_parse_rejects_out_of_range_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence=150))


def test_parse_rejects_negative_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence=-1))


def test_fallback_result_has_confidence_zero():
    assert "confidence" in FALLBACK_RESULT
    assert FALLBACK_RESULT["confidence"] == 0


def test_parse_strips_markdown_fences():
    raw = "```json\n" + _valid_json() + "\n```"
    result = _parse_claude_json(raw)
    assert result["tier"] == "VIP"
    assert result["confidence"] == 87
```

- [ ] **Step 2: Run tests — verify they fail**

```
pytest tests/test_evaluator.py -v
```
Expected: `test_parse_returns_confidence_field` fails (KeyError), `test_fallback_result_has_confidence_zero` fails.

- [ ] **Step 3: Replace tools/claude_evaluator.py**

Replace the full contents of `tools/claude_evaluator.py`:
```python
#!/usr/bin/env python3
"""
Real estate lead evaluation via Claude Sonnet 4.5 Vision.

CLI usage:
    echo '{...}' | python tools/claude_evaluator.py
    python tools/claude_evaluator.py < lead_payload.json

Server usage: imported by tools/api_server.py — call evaluate_lead(lead, tenant).

Input fields (all strings unless noted):
    lead_name             - prospect name from ManyChat
    wa_display_name       - WhatsApp display name ("Private" if unavailable)
    wa_status_text        - WhatsApp "About" text (empty string if unavailable)
    location              - preferred property location / project
    budget_range          - self-reported budget
    timeline              - purchase timeline
    purpose               - "live-in" or "investment"
    carrier               - phone carrier from Twilio Lookup
    country_code          - ISO country code from Twilio Lookup
    phone_valid           - "true" | "false" | "unverified"
    wa_profile_picture_url - publicly accessible image URL (or empty string)

Output JSON:
    tier           - "VIP" | "Medium" | "Low"
    confidence     - integer 0–100
    reasoning      - 2-sentence classification explanation
    visual_signals - profile picture contribution ("none" if absent)
    sales_strategy - actionable advice for the closer

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

# Generic system prompt — classification thresholds are intentionally absent here
# so this block stays identical across all tenants and the cache hit applies to every call.
# Per-client rules (budget, currency, locations) are injected into the user message.
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
    """Assemble the multimodal message content list for the API call."""
    from tools.db import DEFAULT_TENANT
    cfg = tenant if tenant is not None else DEFAULT_TENANT

    vip_locs = ", ".join(cfg.vip_locations)
    rules = (
        "[Classification Rules — apply these exactly]\n"
        f"- VIP Lead: Target areas ({vip_locs}), OR budget >= {cfg.budget_vip_min:,} {cfg.currency}, "
        "OR cash-ready timeline, OR profile picture shows affluence/executive context.\n"
        f"- Medium Lead: Standard residential with payment plan, budget "
        f"{cfg.budget_medium_min:,}-{cfg.budget_vip_min:,} {cfg.currency}, 6-month decision timeline.\n"
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
        content.append({
            "type": "image",
            "source": {"type": "url", "url": pic_url},
        })

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
    """Extract and validate the JSON object from Claude's response."""
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

    if not isinstance(result["confidence"], int) or not (0 <= result["confidence"] <= 100):
        raise ValueError(
            f"Invalid confidence value: {result['confidence']!r} — must be integer 0-100"
        )

    return result


def evaluate_lead(lead: dict, tenant=None) -> dict:
    """
    Send the enriched lead payload to Claude for qualification.

    tenant: TenantConfig from the DB (HTTP server mode), or None to use DEFAULT_TENANT (CLI mode).
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
                    # Cache the stable system prompt. First call pays the write premium (~1.25x);
                    # subsequent calls within the 5-min TTL pay ~0.1x — effective across all tenants
                    # because the system prompt no longer contains per-client thresholds.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_content(lead, tenant),
                }
            ],
        )

        text = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        )
        return _parse_claude_json(text)

    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"[claude_evaluator] Evaluation failed: {exc}\n")
        return FALLBACK_RESULT


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

    result = evaluate_lead(lead)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests — verify they pass**

```
pytest tests/test_evaluator.py -v
```
Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```
git add tools/claude_evaluator.py tests/test_evaluator.py
git commit -m "feat: add confidence scoring and per-tenant config injection to evaluator"
```

---

### Task 4: Tenant provisioning CLI + delete app.py

**Files:**
- Create: `tools/seed_tenant.py`
- Delete: `app.py`

- [ ] **Step 1: Create tools/seed_tenant.py**

Create `tools/seed_tenant.py`:
```python
#!/usr/bin/env python3
"""
One-shot CLI to provision a new tenant API key.
Run once per client onboarding. The key is printed once and never stored in plaintext.

Usage:
    python tools/seed_tenant.py <client_id> <display_name> [options]

Options:
    --budget-vip-min INT       Minimum budget for VIP tier (default: 8000000)
    --budget-medium-min INT    Minimum budget for Medium tier (default: 3000000)
    --currency STR             Currency code (default: EGP)
    --vip-locations STR,...    Comma-separated VIP locations (default: North Coast,...)
    --output-language STR      'en' or 'ar' (default: en)

Examples:
    python tools/seed_tenant.py agency-01 "Alpha Realty"
    python tools/seed_tenant.py agency-ae "Dubai Properties" --currency AED --budget-vip-min 2000000 --output-language ar
"""
import argparse
import secrets
import sys

from tools.db import create_tenant, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a new tenant API key")
    parser.add_argument("client_id", help="Unique client identifier (e.g. agency-01)")
    parser.add_argument("name", help="Display name for this client")
    parser.add_argument("--budget-vip-min", type=int, default=8_000_000)
    parser.add_argument("--budget-medium-min", type=int, default=3_000_000)
    parser.add_argument("--currency", default="EGP")
    parser.add_argument(
        "--vip-locations",
        default="North Coast,New Zayed,Gouna,Golden Square,New Cairo",
    )
    parser.add_argument("--output-language", default="en", choices=["en", "ar"])
    args = parser.parse_args()

    api_key = secrets.token_urlsafe(32)
    init_db()

    try:
        create_tenant(
            args.client_id,
            api_key,
            args.name,
            budget_vip_min=args.budget_vip_min,
            budget_medium_min=args.budget_medium_min,
            currency=args.currency,
            vip_locations=args.vip_locations.split(","),
            output_language=args.output_language,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Tenant created: {args.client_id} ({args.name})")
    print(f"API Key: {api_key}")
    print("Save this key — it is hashed in the database and cannot be recovered.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Delete app.py**

```
# PowerShell
Remove-Item app.py

# or Bash
rm app.py
```

- [ ] **Step 3: Run full test suite — verify all green**

```
pytest tests/ -v
```
Expected: all tests across `test_db.py`, `test_auth.py`, `test_evaluator.py` pass. No references to `app.py` remain.

- [ ] **Step 4: Commit**

```
git add tools/seed_tenant.py
git rm app.py
git commit -m "feat: add tenant provisioning CLI, remove redundant app.py"
```

---

## Self-Review

**Spec coverage:**
| Requirement | Task |
|---|---|
| API auth + tenant isolation | Task 2 (`auth.py`, `api_server.py` Depends) |
| Usage logging | Task 2 (`log_evaluation` in `/evaluate`) |
| Phone hash deduplication with 24h TTL | Task 1 (`is_duplicate`) + Task 2 (wire in server) |
| Confidence scoring | Task 3 (`_parse_claude_json`, `FALLBACK_RESULT`, system prompt) |
| Per-client config (currency, thresholds, locations) | Task 1 (`TenantConfig`) + Task 3 (`_build_user_content`) |
| Arabic output | Task 3 (`output_language` → `lang_note` in user message) |
| Operational bootstrap | Task 4 (`seed_tenant.py`) |
| Delete redundant `app.py` | Task 4 |
| Cache efficiency preserved across tenants | Task 3 (thresholds moved to user message, system prompt stays static) |

**Placeholder scan:** None found — every step contains runnable code and expected output.

**Type consistency:** `TenantConfig` defined in `db.py` (Task 1), used identically in `auth.py` (Task 2), `claude_evaluator.py` (Task 3), and `api_server.py` (Task 2). `evaluate_lead(lead, tenant)` signature matches between evaluator definition (Task 3) and server call site (Task 2) and test mock (Task 2).
