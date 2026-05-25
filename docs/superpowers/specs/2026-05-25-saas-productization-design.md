# SaaS Productization Phase — Design Spec

**Date:** 2026-05-25
**Status:** Approved (revised 2026-05-25)
**Scope:** Google Sheets integration, VIP notifications (Telegram + WhatsApp), lightweight dashboard, tenant config management CLI, usage quota enforcement, webhook support, schema evolution.

---

## Context

The multi-tenant lead evaluation API is stable and pilot-ready. The core loop is:
`POST /evaluate → auth → dedup → Claude Vision → log → return result`

This phase adds the commercial layer on top of that core without modifying its evaluation logic or prompt-cache strategy.

**Constraints carried forward:**
- Prompt cache must remain effective (system prompt stays tenant-agnostic)
- Evaluation response latency must not increase
- No new infrastructure (no Redis, no Celery, no separate worker process)
- SQLite remains the DB — appropriate for 5–20 tenants
- Single server deployment (Docker or bare uvicorn)

---

## Architecture Overview

```
POST /evaluate
  → require_tenant()             # existing auth
  → check_quota()                # NEW: 429 if monthly limit reached
  → dedup check                  # existing
  → evaluate_lead()              # existing (returns result + usage tuple now)
  → log_evaluation()             # extended: stores lead data + tokens
  → BackgroundTasks.add(         # NEW: non-blocking
        fire_integrations()
        ├── sheets_append()
        ├── notify_vip()
        │     ├── telegram_notify()
        │     └── whatsapp_notify()
        └── webhook_post()
    )
  → return EvaluationResult      # unchanged response contract

GET /dashboard                   # NEW: HTML page, same X-API-Key auth
GET /stats                       # extended: adds quota + token fields
```

**Approach:** Thin integration layer (Approach B). All post-evaluation side effects live in `tools/integrations.py`, called via FastAPI `BackgroundTasks`. The evaluation response returns before integrations run. Each integration is independently wrapped in try/except — one failure never blocks others.

---

## Section 1: Schema Evolution

### Migration strategy

`init_db()` runs `ALTER TABLE ADD COLUMN` for each new column at every startup. SQLite raises `OperationalError` if the column already exists — caught and ignored per column. No migration version table, no migration runner. Safe for production: existing rows get the DEFAULT value, no data is touched.

### `evaluations` table — 7 new columns

```sql
ALTER TABLE evaluations ADD COLUMN lead_name    TEXT NOT NULL DEFAULT '';
ALTER TABLE evaluations ADD COLUMN phone_number TEXT NOT NULL DEFAULT '';
ALTER TABLE evaluations ADD COLUMN location     TEXT NOT NULL DEFAULT '';
ALTER TABLE evaluations ADD COLUMN reasoning    TEXT NOT NULL DEFAULT '';
ALTER TABLE evaluations ADD COLUMN source       TEXT NOT NULL DEFAULT 'api';
ALTER TABLE evaluations ADD COLUMN input_tokens       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE evaluations ADD COLUMN output_tokens      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE evaluations ADD COLUMN cache_read_tokens  INTEGER NOT NULL DEFAULT 0;
```

`phone_number` stores the full number — this is the tenant's CRM data on their own server. **Never included in structured log output.** Only used in Sheets rows, VIP notification messages, and webhook payloads. Not shown in the dashboard (dashboard displays last 4 digits only: `****1234`).
`location` enables the top-locations stat and dashboard bar chart.
`reasoning` is stored for Sheets export and VIP notification message body. **Truncated to 500 characters** in `log_evaluation()` before INSERT — keeps DB lean and notifications concise.
`source` is set to `'api'` by the server; reserved for future ManyChat/n8n source tagging.
`input_tokens` / `output_tokens` / `cache_read_tokens` from the Anthropic usage object; used for cost estimation in `/stats` and dashboard.

### `tenants` table — 10 new columns

```sql
ALTER TABLE tenants ADD COLUMN monthly_quota      INTEGER NOT NULL DEFAULT 1000;
ALTER TABLE tenants ADD COLUMN is_active          INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tenants ADD COLUMN sheets_id          TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN telegram_bot_token TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN telegram_chat_id   TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN wa_notify_url      TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN wa_notify_token    TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN wa_notify_to       TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN webhook_url        TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN vip_min_confidence INTEGER NOT NULL DEFAULT 70;
```

`monthly_quota`: auto-block threshold. Default 1000 evaluations/month.
`is_active`: `1` = active, `0` = suspended. Checked in `require_tenant()` — returns HTTP 403 with `{"detail": "Account suspended. Contact support."}`. Preserves all history; no data is deleted on suspension.
`sheets_id`: Google Spreadsheet ID (the long string in the sheet URL). Empty = Sheets disabled.
`telegram_*`: both fields must be non-empty for Telegram to activate.
`wa_notify_*`: all three fields must be non-empty for WhatsApp to activate.
`webhook_url`: empty = webhook disabled.
`vip_min_confidence`: VIP leads below this confidence do not trigger notifications.

### Updated `TenantConfig` dataclass

All 10 new fields added with matching defaults. `get_tenant_by_api_key()` reads them from the row. `create_tenant()` accepts them as kwargs (all optional, fall back to defaults).

`require_tenant()` in `auth.py` gains a second check after the key lookup:
```python
if not tenant.is_active:
    raise HTTPException(status_code=403, detail="Account suspended. Contact support.")
```
This runs for all authenticated endpoints (`/evaluate`, `/stats`, `/dashboard`).

---

## Section 2: Token Tracking & Quota Enforcement

### `evaluate_lead()` return signature change

Before: `evaluate_lead(lead, tenant) -> dict`
After: `evaluate_lead(lead, tenant) -> tuple[dict, dict]`

The second element is a usage dict:
```python
{"input_tokens": int, "output_tokens": int, "cache_read_tokens": int}
```

Extracted from `response.usage` on the Anthropic SDK response object:
- `input_tokens` = `response.usage.input_tokens`
- `output_tokens` = `response.usage.output_tokens`
- `cache_read_tokens` = `response.usage.cache_read_input_tokens` (0 if absent)

`FALLBACK_RESULT` returns `(fallback_dict, {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0})`.

### `log_evaluation()` updated signature

```python
def log_evaluation(
    client_id: str,
    phone_hash: str,
    phone_number: str,   # stored in DB; never logged to stdout/stderr
    lead_name: str,
    location: str,
    tier: str,
    confidence: int,
    reasoning: str,      # truncated to 500 chars before INSERT
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    source: str = "api",
    is_dup: bool = False,
) -> int:  # returns the new row id (needed by integrations)
```

Truncation applied inline: `reasoning = reasoning[:500]` before the INSERT. No external utility needed.

### `check_quota()`

```python
def check_quota(client_id: str, monthly_quota: int) -> None:
    """Raises QuotaExceededError if the tenant has hit their monthly limit."""
```

Count query:
```sql
SELECT COUNT(*) FROM evaluations
WHERE client_id = ?
  AND is_duplicate = 0
  AND evaluated_at >= date('now', 'start of month')
```

If `count >= monthly_quota`, raises a new `QuotaExceededError(used, limit)`. The server catches this and returns HTTP 429:
```json
{"detail": "Monthly quota of 1000 evaluations reached. Contact support to upgrade."}
```

Quota check runs **before** the Claude call — no tokens are burned on over-quota requests.

### `/stats` response — new fields

Each window gains:
```json
"quota": {
  "used": 312,
  "limit": 1000,
  "remaining": 688
},
"tokens": {
  "input": 45000,
  "output": 8000,
  "cache_read": 38000,
  "estimated_usd": 0.12
}
```

Estimated cost formula (Sonnet 4.5 rates, informational only):
```
estimated_usd = (input_tokens * 0.000003)
              + (output_tokens * 0.000015)
              - (cache_read_tokens * 0.0000027)
```

---

## Section 3: Integration Layer

### `tools/integrations.py`

Single public function called from `api_server.py`:

```python
def fire_integrations(
    tenant: TenantConfig,
    lead: dict,
    result: dict,
    eval_id: int,
    is_duplicate: bool,
) -> None:
```

Called via `BackgroundTasks.add_task(fire_integrations, ...)` — runs after the response is sent.

**Internal dispatch:**
```python
def fire_integrations(...):
    _sheets_append(...)    # guarded by tenant.sheets_id
    _notify_vip(...)       # guarded by tier + confidence threshold
    _webhook_post(...)     # guarded by tenant.webhook_url
```

Each sub-function is independently wrapped in `try/except Exception` — logs failure at WARNING level with `tenant_id` and integration name, never propagates. **Phone numbers are never included in log output from any integration function.**

All outbound network calls carry explicit timeouts — no integration can hang and delay the background task worker. Timeouts:
- Google Sheets API: 15 seconds (library default, overridden via `httplib2` transport)
- Telegram `send_message`: 10 seconds (via `read_timeout` and `connect_timeout` kwargs)
- WhatsApp POST: 10 seconds (`httpx.post(..., timeout=10)`)
- Webhook POST: 10 seconds (`httpx.post(..., timeout=10)`)

### Google Sheets (`_sheets_append`)

- Activated when `tenant.sheets_id` is non-empty
- Uses `google-auth` + `google-api-python-client`
- Service account JSON path read from `GOOGLE_SERVICE_ACCOUNT_JSON` env var at import time
- If env var is absent, Sheets integration silently skips with a one-time WARNING log at startup
- Scopes: `['https://www.googleapis.com/auth/spreadsheets']`
- On first append: checks if row 1 is empty, writes header if so
- Header row: `Timestamp | Lead Name | Phone | Tier | Confidence | Reasoning | Source | Duplicate | Location`
- Appends one row per evaluation (including duplicates — marked in the Duplicate column)

### VIP Notifications (`_notify_vip`)

Fires only when:
1. `result["tier"] == "VIP"`
2. `result["confidence"] >= tenant.vip_min_confidence`
3. `is_duplicate == False`

**Notification message template:**
```
🔔 VIP Lead — {tenant.name}
Name: {lead_name}
Phone: {phone_number}        ← full number (operationally required for immediate callback)
Confidence: {confidence}%
Location: {location}
Reasoning: {reasoning}       ← already capped at 500 chars from DB
Action: {sales_strategy}
```

Phone number is included in the notification body (the sales director needs it to call back immediately). It is **not** included in any log.info/log.warning calls within the notification functions.

**Telegram (`_telegram_notify`):**
- Activated when both `telegram_bot_token` and `telegram_chat_id` are non-empty
- Uses `python-telegram-bot` in sync mode (`Bot.send_message`)
- No async complexity needed — this runs in a background task already

**WhatsApp (`_whatsapp_notify`):**
- Activated when `wa_notify_url`, `wa_notify_token`, and `wa_notify_to` are all non-empty
- Plain HTTP POST via `httpx` (already a dependency)
- Request body:
  ```json
  {"to": "<wa_notify_to>", "body": "<message text>"}
  ```
- Authorization header: `Bearer <wa_notify_token>`
- Timeout: 10 seconds

Both notification channels are independent — one failing does not suppress the other.

### Webhook (`_webhook_post`)

- Activated when `tenant.webhook_url` is non-empty
- POST via `httpx`, timeout 10 seconds, no retry
- Payload:
  ```json
  {
    "event": "lead_evaluated",
    "event_id": "a3f7c2d1-84bb-4e9a-b012-3f5e7a8c1d90",
    "tenant_id": "agency-01",
    "timestamp": "2026-05-25T14:23:00Z",
    "lead": {
      "name": "Ahmed Hassan",
      "phone": "+201234567890",
      "location": "North Coast"
    },
    "result": {
      "tier": "VIP",
      "confidence": 87,
      "reasoning": "...",
      "sales_strategy": "..."
    },
    "is_duplicate": false
  }
  ```

`event_id` is a UUID4 generated per webhook call (`uuid.uuid4()`). Enables the receiving CRM/automation to deduplicate retries and trace events in their own logs.

---

## Section 4: Dashboard

### Endpoint

`GET /dashboard` — protected by `X-API-Key` header (same `require_tenant` dependency).

Returns HTML. FastAPI's `Jinja2Templates` serves `tools/templates/dashboard.html`.

If `jinja2` is not installed, the endpoint raises a clear `500` with a message directing the operator to install it — it does not crash the server at startup.

### Data queries

Three new DB functions in `db.py`:

**`get_top_locations(client_id, hours) -> list[dict]`**
```sql
SELECT location, COUNT(*) as count
FROM evaluations
WHERE client_id = ? AND is_duplicate = 0 AND evaluated_at > ?
  AND location != ''
GROUP BY location
ORDER BY count DESC
LIMIT 5
```

**`get_recent_evaluations(client_id, limit=20) -> list[dict]`**
```sql
SELECT lead_name, tier, confidence, location, evaluated_at, is_duplicate,
       '****' || SUBSTR(phone_number, -4) AS phone_masked
FROM evaluations
WHERE client_id = ?
ORDER BY evaluated_at DESC
LIMIT ?
```
Raw `phone_number` is never selected by this query — only the masked form is exposed to the template.

**`get_quota_status(client_id, monthly_quota) -> dict`**
Returns `{"used": N, "limit": M, "remaining": M-N}` — same logic as `check_quota` but read-only.

### Template design

Single HTML file, inline CSS (dark neutral palette, clean table layout), no external dependencies. Vanilla JS for the 24h / 7d / 30d toggle — sends the chosen window as a query param `?window=7d`, page reloads with the new stats. No AJAX, no framework.

**Sections:**
1. Header bar — tenant name + selected window toggle
2. Summary cards — Total | VIP | Duplicates | Avg Confidence | Quota Used
3. Top Locations — horizontal bar chart rendered in pure HTML/CSS (no canvas, no chart lib)
4. Recent Evaluations — table with Lead Name, Tier, Confidence, Location, Time

Phone numbers are never shown in the dashboard. Lead names are shown as-is from the stored value (falls back to `"Unknown"` if empty).

**No frontend frameworks.** No React, no Vue, no SPA architecture, no chart libraries (no Chart.js, no D3). The top-locations bar chart is pure HTML/CSS (`<div>` widths as percentages). The time-window toggle is a plain `<a>` link with `?window=` query param — no JavaScript required for navigation. The only JS in the file is the optional active-tab highlight (3 lines).

---

## Section 5: Tenant Config Management CLI

### `tools/update_tenant.py`

Mirrors `seed_tenant.py` in structure. Accepts `client_id` as first positional argument, then any subset of updatable flags.

**Updatable flags:**

| Flag | DB column |
|------|-----------|
| `--budget-vip-min` | `budget_vip_min` |
| `--budget-medium-min` | `budget_medium_min` |
| `--currency` | `currency` |
| `--vip-locations` | `vip_locations` |
| `--output-language` | `output_language` |
| `--monthly-quota` | `monthly_quota` |
| `--sheets-id` | `sheets_id` |
| `--telegram-bot-token` | `telegram_bot_token` |
| `--telegram-chat-id` | `telegram_chat_id` |
| `--wa-notify-url` | `wa_notify_url` |
| `--wa-notify-token` | `wa_notify_token` |
| `--wa-notify-to` | `wa_notify_to` |
| `--webhook-url` | `webhook_url` |
| `--vip-min-confidence` | `vip_min_confidence` |
| `--suspend` | `is_active = 0` |
| `--activate` | `is_active = 1` |

`--suspend` and `--activate` are mutually exclusive flags (argparse group). Prints `"agency-01 suspended."` or `"agency-01 activated."`.

**Behaviour:**
- Only passed flags are updated (`UPDATE tenants SET col=? WHERE client_id=?` per flag)
- Prints each change: `budget_vip_min: 8000000 → 10000000`
- If `client_id` not found: exits with error, no DB changes
- If no flags passed: prints current config and exits (read-only inspection mode)

---

## Section 6: File Map & Task Order

### New files

| File | Purpose |
|------|---------|
| `tools/integrations.py` | All post-evaluation side effects |
| `tools/update_tenant.py` | Admin CLI for tenant config |
| `tools/templates/dashboard.html` | Jinja2 dashboard template |
| `tests/test_integrations.py` | Integration dispatch unit tests |
| `tests/test_quota.py` | Quota enforcement tests |

### Modified files

| File | Change summary |
|------|---------------|
| `tools/db.py` | Schema migration, new TenantConfig fields, `check_quota()`, `get_top_locations()`, `get_recent_evaluations()`, `get_quota_status()`, updated `log_evaluation()` |
| `tools/claude_evaluator.py` | Return `(result, usage)` tuple |
| `tools/api_server.py` | Wire quota check, BackgroundTasks, dashboard endpoint, updated stats response |
| `tools/requirements.txt` | Add `google-auth`, `google-api-python-client`, `jinja2`, `python-telegram-bot` |
| `.env.example` | Add `GOOGLE_SERVICE_ACCOUNT_JSON` |

### Build order

```
Task 1 — db.py: schema migration + TenantConfig expansion + new query functions
Task 2 — claude_evaluator.py: return (result, usage) tuple
Task 3 — db.py: check_quota(), updated log_evaluation() signature
Task 4 — tools/integrations.py: Sheets, Telegram, WhatsApp, webhook
Task 5 — tools/templates/dashboard.html + GET /dashboard endpoint
Task 6 — tools/update_tenant.py CLI
Task 7 — api_server.py: wire quota, BackgroundTasks, dashboard, extended stats
Task 8 — tests: test_quota.py, test_integrations.py, update existing tests for new signatures
```

Tasks 4, 5, 6 can proceed in parallel after Task 1. Task 7 requires Tasks 1–5.

### Environment variables

```bash
# .env additions
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service_account.json  # optional; Sheets disabled if absent
```

All notification credentials (Telegram tokens, WhatsApp tokens, webhook URLs) live in the DB per tenant via `update_tenant.py`. No per-tenant secrets in `.env`.

---

## Edge Cases & Failure Modes

| Scenario | Behaviour |
|----------|-----------|
| Sheets API down | Logged at WARNING, evaluation unaffected |
| Telegram rate limit | Caught, logged, no retry |
| WhatsApp API timeout | 10s timeout, caught, logged |
| Webhook URL returns 500 | Logged at WARNING, no retry |
| Missing `GOOGLE_SERVICE_ACCOUNT_JSON` | One-time WARNING at startup, Sheets silently skipped |
| VIP lead below confidence threshold | No notification sent |
| Duplicate lead | Logged to Sheets (marked), no VIP notification |
| Quota reached | HTTP 429 before Claude call — zero tokens burned |
| Tenant suspended (`is_active=0`) | HTTP 403 on all authenticated endpoints, history preserved |
| Schema column already exists on restart | `OperationalError` caught per column, startup continues |
| phone_number empty (legacy row) | `phone_masked` returns `'****'`, no crash |
| reasoning > 500 chars from Claude | Truncated to 500 in `log_evaluation()` before INSERT |

---

## Out of Scope (This Phase)

- Retry queues for failed integrations (add per-integration if production need arises)
- Self-serve agency onboarding portal
- Multiple sheet tabs per tenant
- Notification templates customisable per tenant
- WhatsApp template messages (HSM) — current design uses session messages via Whapi/Wassenger
- Per-evaluation source tagging beyond `'api'` default
