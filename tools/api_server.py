#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

POST /evaluate  — requires X-API-Key header, rate-limited 60 req/min per key
GET  /stats     — requires X-API-Key header
GET  /dashboard — requires X-API-Key header, returns HTML
GET  /health    — liveness check, no auth required

Start:
    uvicorn tools.api_server:app --host 0.0.0.0 --port 8000

Docker:
    docker run -p 8000:8000 -e ANTHROPIC_API_KEY=... -v /data:/app/data lead-evaluator
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
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from tools.auth import require_admin, require_tenant
from tools.claude_evaluator import evaluate_lead
from tools.db import (
    QuotaExceededError,
    TenantConfig,
    check_quota,
    create_tenant,
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

# cache_size=0 works around a Python 3.14 + Jinja2 LRU cache hash bug
_jinja_env = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
    cache_size=0,
)
templates = Jinja2Templates(env=_jinja_env)


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


class TenantCreateRequest(BaseModel):
    client_id: str
    name: str
    budget_vip_min: int = 8_000_000
    budget_medium_min: int = 3_000_000
    currency: str = "EGP"
    vip_locations: list[str] = ["North Coast", "New Zayed", "Gouna", "Golden Square", "New Cairo"]
    output_language: str = "en"
    monthly_quota: int = 1000


class TenantCreateResponse(BaseModel):
    client_id: str
    api_key: str
    message: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/admin/tenants", response_model=TenantCreateResponse, status_code=201)
def admin_create_tenant(
    body: TenantCreateRequest,
    _: None = Depends(require_admin),
) -> TenantCreateResponse:
    import secrets as _secrets
    api_key = _secrets.token_urlsafe(32)
    try:
        create_tenant(
            body.client_id,
            api_key,
            body.name,
            budget_vip_min=body.budget_vip_min,
            budget_medium_min=body.budget_medium_min,
            currency=body.currency,
            vip_locations=body.vip_locations,
            output_language=body.output_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("admin", extra={"action": "create_tenant", "client_id": body.client_id})
    return TenantCreateResponse(
        client_id=body.client_id,
        api_key=api_key,
        message="Save this key — it is hashed in the database and cannot be recovered.",
    )


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
        request,
        "dashboard.html",
        context={
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
