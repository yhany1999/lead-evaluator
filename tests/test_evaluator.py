import json
import pytest
from tools.claude_evaluator import FALLBACK_RESULT, _build_user_content, _parse_claude_json
from tools.db import TenantConfig


# ── helpers ──────────────────────────────────────────────────────────────────

def _valid_json(**overrides) -> str:
    base = {
        "tier": "VIP",
        "confidence": 87,
        "reasoning": "High budget and VIP location.",
        "visual_signals": "Professional attire visible.",
        "sales_strategy": "Assign to senior closer immediately.",
    }
    base.update(overrides)
    return json.dumps(base)


def _custom_tenant(**kwargs) -> TenantConfig:
    defaults = dict(
        client_id="agency-ae",
        name="Dubai Properties",
        budget_vip_min=2_000_000,
        budget_medium_min=800_000,
        currency="AED",
        vip_locations=["Palm Jumeirah", "Downtown Dubai"],
        output_language="en",
    )
    defaults.update(kwargs)
    return TenantConfig(**defaults)


# ── _parse_claude_json ────────────────────────────────────────────────────────

def test_parse_returns_confidence_field():
    result = _parse_claude_json(_valid_json())
    assert "confidence" in result
    assert result["confidence"] == 87


def test_parse_rejects_non_integer_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence="high"))


def test_parse_rejects_out_of_range_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence=150))


def test_parse_rejects_negative_confidence():
    with pytest.raises(ValueError, match="confidence"):
        _parse_claude_json(_valid_json(confidence=-1))


def test_parse_strips_markdown_fences():
    raw = "```json\n" + _valid_json() + "\n```"
    result = _parse_claude_json(raw)
    assert result["tier"] == "VIP"
    assert result["confidence"] == 87


def test_parse_strips_plain_code_fences():
    raw = "```\n" + _valid_json() + "\n```"
    result = _parse_claude_json(raw)
    assert result["tier"] == "VIP"


def test_parse_rejects_invalid_tier():
    with pytest.raises(ValueError, match="tier"):
        _parse_claude_json(_valid_json(tier="Premium"))


def test_parse_rejects_missing_required_keys():
    incomplete = json.dumps({"tier": "VIP", "confidence": 80})
    with pytest.raises(ValueError, match="missing keys"):
        _parse_claude_json(incomplete)


# ── FALLBACK_RESULT ───────────────────────────────────────────────────────────

def test_fallback_result_has_confidence_zero():
    assert "confidence" in FALLBACK_RESULT
    assert FALLBACK_RESULT["confidence"] == 0


def test_fallback_result_has_all_required_keys():
    required = ("tier", "confidence", "reasoning", "visual_signals", "sales_strategy")
    for key in required:
        assert key in FALLBACK_RESULT, f"FALLBACK_RESULT missing '{key}'"


def test_fallback_result_tier_is_medium():
    assert FALLBACK_RESULT["tier"] == "Medium"


# ── _build_user_content — tenant config injection ─────────────────────────────

def test_build_injects_tenant_vip_budget_threshold():
    tenant = _custom_tenant()
    lead = {"lead_name": "Sara"}
    content = _build_user_content(lead, tenant)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    assert "2,000,000" in text_block
    assert "AED" in text_block


def test_build_injects_tenant_medium_budget_threshold():
    tenant = _custom_tenant()
    lead = {"lead_name": "Omar"}
    content = _build_user_content(lead, tenant)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    assert "800,000" in text_block


def test_build_injects_tenant_vip_locations():
    tenant = _custom_tenant()
    lead = {"lead_name": "Khalid"}
    content = _build_user_content(lead, tenant)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    assert "Palm Jumeirah" in text_block
    assert "Downtown Dubai" in text_block


def test_build_injects_arabic_language_instruction():
    tenant = _custom_tenant(output_language="ar")
    lead = {"lead_name": "Ali"}
    content = _build_user_content(lead, tenant)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    assert "Arabic" in text_block


def test_build_no_arabic_instruction_for_english_tenant():
    tenant = _custom_tenant(output_language="en")
    lead = {"lead_name": "Ahmed"}
    content = _build_user_content(lead, tenant)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    assert "Arabic" not in text_block


def test_build_uses_default_tenant_when_none():
    lead = {"lead_name": "Nour"}
    content = _build_user_content(lead, tenant=None)
    text_block = next(b["text"] for b in content if b["type"] == "text")
    # Default tenant is EGP with 8M VIP threshold
    assert "EGP" in text_block
    assert "8,000,000" in text_block


def test_build_attaches_image_block_when_url_present():
    tenant = _custom_tenant()
    lead = {"wa_profile_picture_url": "https://example.com/pic.jpg"}
    content = _build_user_content(lead, tenant)
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["source"]["url"] == "https://example.com/pic.jpg"


def test_build_no_image_block_when_url_absent():
    tenant = _custom_tenant()
    lead = {"lead_name": "Test"}
    content = _build_user_content(lead, tenant)
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 0


# ── system prompt cache invariant ─────────────────────────────────────────────

def test_system_prompt_contains_no_hardcoded_currency():
    from tools.claude_evaluator import SYSTEM_PROMPT
    # Thresholds must live in the user message, not the system prompt,
    # so the cache token is identical across all tenants.
    assert "EGP" not in SYSTEM_PROMPT
    assert "AED" not in SYSTEM_PROMPT
    assert "8,000,000" not in SYSTEM_PROMPT
    assert "8M" not in SYSTEM_PROMPT


def test_evaluate_lead_returns_tuple(monkeypatch):
    import anthropic
    from tools.claude_evaluator import evaluate_lead

    class _FakeUsage:
        input_tokens = 120
        output_tokens = 48
        cache_read_input_tokens = 95

    class _FakeContent:
        type = "text"
        text = json.dumps({
            "tier": "VIP",
            "confidence": 88,
            "reasoning": "High budget.",
            "visual_signals": "none",
            "sales_strategy": "Assign to senior closer.",
        })

    class _FakeResponse:
        content = [_FakeContent()]
        usage = _FakeUsage()

    class _FakeClient:
        def __init__(self, **kwargs): pass
        class messages:
            @staticmethod
            def create(**kwargs):
                return _FakeResponse()

    monkeypatch.setattr("tools.claude_evaluator.anthropic.Anthropic", _FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result, usage = evaluate_lead({"lead_name": "Ahmed"})
    assert result["tier"] == "VIP"
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 48
    assert usage["cache_read_tokens"] == 95


def test_evaluate_lead_fallback_returns_tuple(monkeypatch):
    import anthropic
    from tools.claude_evaluator import evaluate_lead, FALLBACK_RESULT

    class _FakeClient:
        def __init__(self, **kwargs): pass
        class messages:
            @staticmethod
            def create(**kwargs):
                raise anthropic.APITimeoutError(request=None)

    monkeypatch.setattr("tools.claude_evaluator.anthropic.Anthropic", _FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result, usage = evaluate_lead({"lead_name": "Ahmed"})
    assert result == FALLBACK_RESULT
    assert usage == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
