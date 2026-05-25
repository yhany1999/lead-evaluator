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
