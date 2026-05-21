#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

POST /evaluate  — requires X-API-Key header, rate-limited 60 req/min per key
GET  /stats     — requires X-API-Key header
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

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from tools.auth import require_tenant
from tools.claude_evaluator import evaluate_lead
from tools.db import TenantConfig, get_stats_window, hash_phone, init_db, is_duplicate, log_evaluation
from tools.logging_config import configure_logging

load_dotenv()

log = logging.getLogger(__name__)


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


app = FastAPI(title="Lead Evaluator", version="2.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(_RequestLogger)


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/stats", response_model=StatsResponse)
def stats(tenant: TenantConfig = Depends(require_tenant)) -> StatsResponse:
    try:
        return StatsResponse(
            client_id=tenant.client_id,
            windows={
                "last_24h": WindowStats(**get_stats_window(tenant.client_id, 24)),
                "last_7d":  WindowStats(**get_stats_window(tenant.client_id, 168)),
                "last_30d": WindowStats(**get_stats_window(tenant.client_id, 720)),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="stats unavailable") from exc


@app.post("/evaluate", response_model=EvaluationResult)
@limiter.limit("60/minute")
def evaluate(
    request: Request,
    payload: LeadPayload,
    tenant: TenantConfig = Depends(require_tenant),
) -> EvaluationResult:
    request_id = getattr(request.state, "request_id", "-")
    phone_hash = hash_phone(payload.phone_number) if payload.phone_number else ""

    if phone_hash and is_duplicate(tenant.client_id, phone_hash):
        log_evaluation(tenant.client_id, phone_hash, "Medium", 0, is_dup=True)
        log.info(
            "evaluation",
            extra={
                "request_id": request_id,
                "tenant_id": tenant.client_id,
                "tier": "Medium",
                "confidence": 0,
                "is_duplicate": True,
            },
        )
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

    log.info(
        "evaluation",
        extra={
            "request_id": request_id,
            "tenant_id": tenant.client_id,
            "tier": result["tier"],
            "confidence": result.get("confidence", 0),
            "is_duplicate": False,
        },
    )
    return EvaluationResult(**result)
