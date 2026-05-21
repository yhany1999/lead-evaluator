# Stats Endpoint Design

**Date:** 2026-05-21
**Feature:** `GET /stats` — per-tenant usage reporting
**Status:** Approved, pending implementation

---

## Goal

Give each agency a live JSON endpoint that shows how many leads were evaluated across three rolling time windows. Consumed directly by n8n, a dashboard tool, or a simple curl call. No UI, no login page, no new auth system.

---

## Approach

Add one route (`GET /stats`) to the existing FastAPI server (`tools/api_server.py`), protected by the same `Depends(require_tenant)` dependency already used on `/evaluate`. Add one DB query function (`get_stats_window`) to `tools/db.py`. No new files, no new dependencies, no schema changes.

---

## Data Layer — `tools/db.py`

### New function: `get_stats_window(client_id: str, hours: int) -> dict`

Queries the `evaluations` table for rows belonging to `client_id` where `evaluated_at > now - hours`.

Returns a dict with:

| Field | Definition |
|---|---|
| `total` | All rows in the window (real + duplicate) |
| `vip` | Rows where `tier = 'VIP'` and `is_duplicate = 0` |
| `medium` | Rows where `tier = 'Medium'` and `is_duplicate = 0` |
| `low` | Rows where `tier = 'Low'` and `is_duplicate = 0` |
| `duplicates` | Rows where `is_duplicate = 1` |
| `avg_confidence` | Average `confidence` of real evaluations (`is_duplicate = 0`), rounded to nearest int. Returns `0` if no real evaluations exist in the window. |

**Time window:** Rolling — always `hours` hours back from `datetime.now(timezone.utc)`. Never aligned to calendar months or weeks. `last_24h` = 24 hours, `last_7d` = 168 hours, `last_30d` = 720 hours.

**Empty result:** Returns zeroed dict (all fields = 0) when no rows match. Never raises.

---

## API Layer — `tools/api_server.py`

### New route: `GET /stats`

```
GET /stats
Headers: X-API-Key: <tenant api key>
```

Auth: same `Depends(require_tenant)` as `/evaluate`. Returns 401 if key invalid, 422 if header missing.

Calls `get_stats_window(tenant.client_id, hours)` three times:
- `last_24h` → hours=24
- `last_7d`  → hours=168
- `last_30d` → hours=720

### Response models

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

### Example response

```json
{
  "client_id": "agency-01",
  "windows": {
    "last_24h": { "total": 12, "vip": 3, "medium": 7, "low": 1, "duplicates": 1, "avg_confidence": 74 },
    "last_7d":  { "total": 89, "vip": 21, "medium": 51, "low": 9, "duplicates": 8, "avg_confidence": 71 },
    "last_30d": { "total": 312, "vip": 78, "medium": 180, "low": 32, "duplicates": 22, "avg_confidence": 69 }
  }
}
```

No query parameters. No pagination.

---

## Testing — `tests/test_stats.py`

All tests use `TestClient` with a real SQLite DB via an inline `client` fixture (monkeypatched `DB_PATH` to `tmp_path`, same pattern as `tests/test_auth.py`). No mocks.

| Test | Assertion |
|---|---|
| Valid key returns 200 with all three windows | Response has `last_24h`, `last_7d`, `last_30d` keys |
| Missing API key returns 422 | Auth enforced |
| Invalid API key returns 401 | Auth enforced |
| Empty DB returns zeroed counts | All fields = 0, no crash |
| VIP/Medium/Low counts exclude duplicates | Insert 1 real VIP + 1 duplicate VIP → `vip=1`, `duplicates=1` |
| Records outside window are excluded | Insert row with `evaluated_at` 25h ago → not in `last_24h` |
| Cross-tenant isolation | Agency-01 data not visible to agency-02 |
| `avg_confidence` rounds correctly | Insert rows with confidence 80 and 90 → `avg_confidence=85` |

---

## File Map

| File | Change |
|---|---|
| `tools/db.py` | Add `get_stats_window(client_id, hours) -> dict` |
| `tools/api_server.py` | Add `WindowStats`, `StatsResponse` models; add `GET /stats` route |
| `tests/test_stats.py` | Create — 8 integration tests |

No other files touched.

---

## Out of Scope

- Calendar-based windows (this month, this week) — rolling windows are simpler and consistent
- Super-admin view across all tenants — no clients yet, add when needed
- Google Sheets export — can be built later reading the same `evaluations` table
- Charts or HTML UI — JSON endpoint only; UI is a separate concern
