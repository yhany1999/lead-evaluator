# Lead Evaluator

AI-powered inbound lead qualification for real estate agencies. Evaluates WhatsApp leads via Claude, scores them into VIP / Medium / Low tiers, and fires post-evaluation integrations (Google Sheets, Telegram, WhatsApp, webhook).

## Features

- **Multi-tenant** — each agency gets an isolated API key, quota, and configuration
- **AI evaluation** — Claude analyzes lead profile signals and returns tier, confidence score, reasoning, and a tailored sales strategy
- **Dedup protection** — phones evaluated within 24 hours are flagged as duplicates without burning quota
- **Monthly quota** — configurable per tenant, enforced before any Claude call
- **Integrations** — Google Sheets export, Telegram alerts, WhatsApp notifications, generic webhook
- **Dashboard** — per-tenant HTML dashboard with tier breakdown, token cost, top locations, and recent evaluations
- **Rate limiting** — 60 requests/minute per API key via slowapi
- **Structured logging** — JSON logs with request ID, tenant, tier, duration

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/evaluate` | `X-API-Key` | Evaluate a lead |
| `GET` | `/stats` | `X-API-Key` | JSON stats for 24h / 7d / 30d windows |
| `GET` | `/dashboard` | `X-API-Key` | HTML dashboard (`?window=24h\|7d\|30d`) |
| `GET` | `/health` | none | Liveness check |

## Quick Start

```bash
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env

pip install -r tools/requirements.txt

# Create a tenant
python -m tools.seed_tenant --client-id my-agency --api-key secret123 --name "My Agency"

# Start the server
uvicorn tools.api_server:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t lead-evaluator .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v $(pwd)/data:/app/data \
  lead-evaluator
```

## Evaluate a Lead

```bash
curl -X POST http://localhost:8000/evaluate \
  -H "X-API-Key: secret123" \
  -H "Content-Type: application/json" \
  -d '{
    "lead_name": "Ahmed Hassan",
    "location": "North Coast",
    "budget_range": "5M-8M EGP",
    "timeline": "3 months",
    "purpose": "Investment",
    "phone_number": "+201001234567"
  }'
```

Response:

```json
{
  "tier": "VIP",
  "confidence": 85,
  "reasoning": "North Coast location with strong budget...",
  "visual_signals": "none",
  "sales_strategy": "Prioritize immediate follow-up...",
  "is_duplicate": false
}
```

## Tenant Management

```bash
# Inspect a tenant
python -m tools.update_tenant --client-id my-agency

# Update quota and VIP threshold
python -m tools.update_tenant --client-id my-agency --monthly-quota 500 --vip-min-confidence 75

# Suspend / reactivate
python -m tools.update_tenant --client-id my-agency --suspend
python -m tools.update_tenant --client-id my-agency --activate
```

## Integrations

Set per-tenant via `update_tenant`:

| Integration | Fields |
|-------------|--------|
| Google Sheets | `--sheets-id` + `GOOGLE_SERVICE_ACCOUNT_JSON` env var |
| Telegram | `--telegram-bot-token`, `--telegram-chat-id` |
| WhatsApp | `--wa-notify-url`, `--wa-notify-token`, `--wa-notify-to` |
| Webhook | `--webhook-url` |

VIP notifications fire only when `tier == VIP` and `confidence >= vip_min_confidence`.

## Running Tests

```bash
pytest tests/ -v
```

78 tests, all passing.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` (default: `INFO`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | No | Path to service account JSON for Sheets integration |

## Stack

- **FastAPI** + **uvicorn** — HTTP layer
- **SQLite** (WAL mode) — per-tenant storage
- **Claude** (`claude-opus-4-5` via Anthropic SDK) — lead evaluation
- **slowapi** — rate limiting
- **Jinja2** — dashboard templating
