# Stats Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /stats` to the existing FastAPI server returning per-tenant lead counts and avg confidence across three rolling time windows (24h, 7d, 30d).

**Architecture:** One new DB query function (`get_stats_window`) in `tools/db.py` runs a single SQL aggregate per window. One new route in `tools/api_server.py` calls it three times and returns a typed Pydantic response. Auth reuses the existing `Depends(require_tenant)` — zero new auth code.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (`sqlite3` stdlib), Pydantic, pytest, httpx

---

## File Map

| File | Action | Change |
|---|---|---|
| `tools/db.py` | Modify | Add `get_stats_window(client_id, hours) -> dict` |
| `tools/api_server.py` | Modify | Add `WindowStats`, `StatsResponse` models; add `GET /stats` route; add `get_stats_window` to import |
| `tests/test_stats.py` | Create | 12 tests — 6 direct DB tests (Task 1), 6 API integration tests (Task 2) |

---

### Task 1: DB query function

**Files:**
- Modify: `tools/db.py`
- Create: `tests/test_stats.py`

- [ ] **Step 1: Write failing DB tests**

Create `tests/test_stats.py` with the following content:

```python
import sqlite3
import pytest
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
    conn = sqlite3.connect(str(db_module.DB_PATH))
    conn.execute(
        """INSERT INTO evaluations
           (client_id, phone_hash, tier, confidence, is_duplicate, evaluated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now', '-25 hours'))""",
        ("agency-01", hash_phone("+1"), "VIP", 90, 0),
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
```

- [ ] **Step 2: Run tests — verify they fail**

```
pytest tests/test_stats.py -v
```

Expected: all 6 fail with `ImportError: cannot import name 'get_stats_window' from 'tools.db'`

- [ ] **Step 3: Add `get_stats_window` to `tools/db.py`**

Append this function at the end of `tools/db.py` (after `create_tenant`):

```python
def get_stats_window(client_id: str, hours: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(sep=" ")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                                               AS total,
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
        "avg_confidence": round(avg_conf) if avg_conf is not None else 0,
    }
```

- [ ] **Step 4: Run tests — verify they pass**

```
pytest tests/test_stats.py -v
```

Expected: all 6 pass.

- [ ] **Step 5: Commit**

```
git add tools/db.py tests/test_stats.py
git commit -m "feat: add get_stats_window DB query for rolling-window lead stats"
```

---

### Task 2: API route + integration tests

**Files:**
- Modify: `tools/api_server.py`
- Modify: `tests/test_stats.py`

- [ ] **Step 1: Add failing API integration tests**

Append the following to `tests/test_stats.py` (after the existing 6 tests):

```python
import pytest
from fastapi.testclient import TestClient
from tools import db as db_module
from tools.db import create_tenant, init_db, log_evaluation, hash_phone


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


def test_stats_missing_key_returns_422(client):
    resp = client.get("/stats")
    assert resp.status_code == 422


def test_stats_invalid_key_returns_401(client):
    resp = client.get("/stats", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_stats_empty_db_returns_zeroed_counts(client):
    resp = client.get("/stats", headers={"X-API-Key": "valid-key"})
    window = resp.json()["windows"]["last_24h"]
    assert window == {
        "total": 0, "vip": 0, "medium": 0,
        "low": 0, "duplicates": 0, "avg_confidence": 0,
    }


def test_stats_counts_reflect_evaluations(client):
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 90)
    log_evaluation("agency-01", hash_phone("+2"), "VIP", 0, is_dup=True)
    resp = client.get("/stats", headers={"X-API-Key": "valid-key"})
    w = resp.json()["windows"]["last_24h"]
    assert w["vip"] == 1
    assert w["duplicates"] == 1
    assert w["total"] == 2


def test_stats_cross_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "isolated.db")
    init_db()
    create_tenant("agency-01", "key-one", "Alpha")
    create_tenant("agency-02", "key-two", "Beta")
    log_evaluation("agency-01", hash_phone("+1"), "VIP", 88)
    from tools.api_server import app
    with TestClient(app) as c:
        resp = c.get("/stats", headers={"X-API-Key": "key-two"})
    assert resp.status_code == 200
    assert resp.json()["windows"]["last_24h"]["total"] == 0
```

- [ ] **Step 2: Run new tests — verify they fail**

```
pytest tests/test_stats.py::test_stats_returns_200_with_all_windows tests/test_stats.py::test_stats_missing_key_returns_422 tests/test_stats.py::test_stats_invalid_key_returns_401 tests/test_stats.py::test_stats_empty_db_returns_zeroed_counts tests/test_stats.py::test_stats_counts_reflect_evaluations tests/test_stats.py::test_stats_cross_tenant_isolation -v
```

Expected: all 6 fail with `404 Not Found` or `AttributeError` — `/stats` route does not exist yet.

- [ ] **Step 3: Add models and route to `tools/api_server.py`**

Replace the import line at the top of `tools/api_server.py`:

```python
from tools.db import TenantConfig, hash_phone, get_stats_window, init_db, is_duplicate, log_evaluation
```

Add these two model classes after `EvaluationResult` (before the `@app.get("/health")` line):

```python
class WindowStats(BaseModel):
    total: int
    vip: int
    medium: int
    low: int
    duplicates: int
    avg_confidence: int


class StatsResponse(BaseModel):
    client_id: str
    windows: dict[str, WindowStats]
```

Add this route after the `/health` route:

```python
@app.get("/stats", response_model=StatsResponse)
def stats(tenant: TenantConfig = Depends(require_tenant)) -> StatsResponse:
    return StatsResponse(
        client_id=tenant.client_id,
        windows={
            "last_24h": WindowStats(**get_stats_window(tenant.client_id, 24)),
            "last_7d":  WindowStats(**get_stats_window(tenant.client_id, 168)),
            "last_30d": WindowStats(**get_stats_window(tenant.client_id, 720)),
        },
    )
```

- [ ] **Step 4: Run new tests — verify they pass**

```
pytest tests/test_stats.py -v
```

Expected: all 12 pass.

- [ ] **Step 5: Run full test suite — verify no regressions**

```
pytest tests/ -v
```

Expected: all 55 tests pass, 0 warnings.

- [ ] **Step 6: Commit**

```
git add tools/api_server.py tests/test_stats.py
git commit -m "feat: add GET /stats endpoint with per-tenant rolling window reporting"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `get_stats_window(client_id, hours) -> dict` | Task 1 Step 3 |
| `total` = real + duplicate | Task 1 Step 3 (COUNT(*)) |
| `vip/medium/low` exclude duplicates | Task 1 Step 3 (CASE WHEN is_duplicate=0) |
| `duplicates` counted separately | Task 1 Step 3 (CASE WHEN is_duplicate=1) |
| `avg_confidence` real evals only, rounded, 0 if empty | Task 1 Step 3 |
| Rolling window cutoff from `datetime.now(timezone.utc)` | Task 1 Step 3 |
| `GET /stats` route | Task 2 Step 3 |
| `WindowStats` + `StatsResponse` Pydantic models | Task 2 Step 3 |
| Auth via `Depends(require_tenant)` — 401/422 | Task 2 Step 3 |
| Three windows: last_24h (24h), last_7d (168h), last_30d (720h) | Task 2 Step 3 |
| Cross-tenant isolation | Task 1 test + Task 2 test |
| Empty DB returns zeroed counts, no crash | Task 1 test + Task 2 test |

**Placeholder scan:** None found. Every step has runnable code and expected output.

**Type consistency:**
- `get_stats_window` returns `dict` with keys: `total, vip, medium, low, duplicates, avg_confidence` (Task 1 Step 3)
- `WindowStats(**get_stats_window(...))` in Task 2 Step 3 — fields match exactly
- `get_stats_window` imported in `api_server.py` via the updated import line in Task 2 Step 3 ✓
