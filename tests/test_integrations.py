import pytest
from unittest.mock import patch, MagicMock, call
from tools.db import TenantConfig
from tools.integrations import fire_integrations, _notify_vip, _webhook_post, _telegram_notify, _whatsapp_notify


def _tenant(**kwargs) -> TenantConfig:
    defaults = dict(
        client_id="agency-01", name="Alpha Realty",
        budget_vip_min=8_000_000, budget_medium_min=3_000_000,
        currency="EGP", vip_locations=["North Coast"],
        output_language="en", monthly_quota=1000, is_active=True,
        sheets_id="", telegram_bot_token="", telegram_chat_id="",
        wa_notify_url="", wa_notify_token="", wa_notify_to="",
        webhook_url="", vip_min_confidence=70,
    )
    defaults.update(kwargs)
    return TenantConfig(**defaults)


VIP = {"tier": "VIP", "confidence": 85, "reasoning": "High budget.", "sales_strategy": "Act now.", "visual_signals": "none"}
LEAD = {"lead_name": "Ahmed Hassan", "phone_number": "+201234567890", "location": "North Coast"}


def test_webhook_fires_when_url_set():
    tenant = _tenant(webhook_url="https://crm.example.com/hook")
    with patch("tools.integrations.httpx.post") as mock_post:
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_called_once()
    payload = mock_post.call_args.kwargs["json"]
    assert payload["event"] == "lead_evaluated"
    assert "event_id" in payload
    assert payload["tenant_id"] == "agency-01"
    assert payload["is_duplicate"] is False
    assert payload["lead"]["name"] == "Ahmed Hassan"
    assert payload["result"]["tier"] == "VIP"


def test_webhook_skips_when_no_url():
    tenant = _tenant(webhook_url="")
    with patch("tools.integrations.httpx.post") as mock_post:
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_not_called()


def test_webhook_failure_does_not_raise():
    tenant = _tenant(webhook_url="https://crm.example.com/hook")
    with patch("tools.integrations.httpx.post", side_effect=Exception("timeout")):
        _webhook_post(tenant, LEAD, VIP, is_duplicate=False)  # must not raise


def test_telegram_fires_when_configured():
    tenant = _tenant(telegram_bot_token="7123:AAF", telegram_chat_id="-1001234")
    with patch("tools.integrations.httpx.post") as mock_post:
        _telegram_notify(tenant, "test message")
    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    assert "7123:AAF" in url
    assert mock_post.call_args.kwargs["json"]["chat_id"] == "-1001234"
    assert mock_post.call_args.kwargs["json"]["text"] == "test message"


def test_telegram_skips_when_not_configured():
    tenant = _tenant(telegram_bot_token="", telegram_chat_id="")
    with patch("tools.integrations.httpx.post") as mock_post:
        _telegram_notify(tenant, "test message")
    mock_post.assert_not_called()


def test_whatsapp_fires_when_configured():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok123",
        wa_notify_to="+20100000000",
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _whatsapp_notify(tenant, "test message")
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["json"]["to"] == "+20100000000"
    assert mock_post.call_args.kwargs["json"]["body"] == "test message"
    assert "tok123" in mock_post.call_args.kwargs["headers"]["Authorization"]


def test_vip_notify_fires_for_vip_high_confidence():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
        vip_min_confidence=70,
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, VIP, is_duplicate=False)
    mock_post.assert_called_once()


def test_vip_notify_skips_low_confidence():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
        vip_min_confidence=70,
    )
    low_result = {**VIP, "confidence": 50}
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, low_result, is_duplicate=False)
    mock_post.assert_not_called()


def test_vip_notify_skips_non_vip_tier():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    medium_result = {**VIP, "tier": "Medium"}
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, medium_result, is_duplicate=False)
    mock_post.assert_not_called()


def test_vip_notify_skips_duplicate():
    tenant = _tenant(
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    with patch("tools.integrations.httpx.post") as mock_post:
        _notify_vip(tenant, LEAD, VIP, is_duplicate=True)
    mock_post.assert_not_called()


def test_fire_integrations_one_failure_does_not_block_others():
    tenant = _tenant(
        webhook_url="https://crm.example.com/hook",
        wa_notify_url="https://gate.whapi.cloud/messages/text",
        wa_notify_token="tok", wa_notify_to="+20100000000",
    )
    call_count = 0

    def flaky_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("first call fails")
        return MagicMock(status_code=200)

    with patch("tools.integrations.httpx.post", side_effect=flaky_post):
        fire_integrations(tenant, LEAD, VIP, eval_id=1, is_duplicate=False)

    assert call_count == 2  # webhook failed, WhatsApp still fired
