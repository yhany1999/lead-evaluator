#!/usr/bin/env python3
"""
HTTP wrapper around claude_evaluator.evaluate_lead().

Exposes a single POST /evaluate endpoint that cloud n8n calls
via an HTTP Request node after Twilio + WhatsApp enrichment.

Start:
    uvicorn tools.api_server:app --host 0.0.0.0 --port 8000

n8n HTTP Request node config:
    Method : POST
    URL    : http://<your-host>:8000/evaluate
    Body   : JSON (pass the enriched lead object)
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from tools.claude_evaluator import evaluate_lead

app = FastAPI(title="Lead Evaluator", version="1.0.0")


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
    wa_profile_picture_url: str = ""


class EvaluationResult(BaseModel):
    tier: str
    reasoning: str
    visual_signals: str
    sales_strategy: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/evaluate", response_model=EvaluationResult)
def evaluate(payload: LeadPayload):
    try:
        result = evaluate_lead(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return result
