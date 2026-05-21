#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

POST /evaluate  — requires X-API-Key header (tenant API key)
GET  /health    — liveness check, no auth required

Start:
    uvicorn tools.api_server:app --host 0.0.0.0 --port 8000

n8n HTTP Request node config:
    Method  : POST
    URL     : http://<your-host>:8000/evaluate
    Headers : X-API-Key: <tenant api key>
    Body    : JSON (enriched lead object including phone_number for dedup)
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from tools.auth import require_tenant
from tools.claude_evaluator import evaluate_lead
from tools.db import TenantConfig, hash_phone, init_db, is_duplicate, log_evaluation


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Lead Evaluator", version="2.0.0", lifespan=lifespan)


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
