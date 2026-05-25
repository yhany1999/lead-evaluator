#!/usr/bin/env python3
"""
Real estate lead evaluation via Claude Sonnet 4.5 Vision.

CLI usage:
    echo '{...}' | python tools/claude_evaluator.py
    python tools/claude_evaluator.py < lead_payload.json

Server usage: imported by tools/api_server.py — call evaluate_lead(lead, tenant).

Input fields (all strings unless noted):
    lead_name             - prospect name from ManyChat
    wa_display_name       - WhatsApp display name ("Private" if unavailable)
    wa_status_text        - WhatsApp "About" text (empty string if unavailable)
    location              - preferred property location / project
    budget_range          - self-reported budget
    timeline              - purchase timeline
    purpose               - "live-in" or "investment"
    carrier               - phone carrier from Twilio Lookup
    country_code          - ISO country code from Twilio Lookup
    phone_valid           - "true" | "false" | "unverified"
    wa_profile_picture_url - publicly accessible image URL (or empty string)

Output JSON:
    tier           - "VIP" | "Medium" | "Low"
    confidence     - integer 0-100
    reasoning      - 2-sentence classification explanation
    visual_signals - profile picture contribution ("none" if absent)
    sales_strategy - actionable advice for the closer

Exit codes (CLI only):
    0  success (including fallback result on API failure)
    1  invalid JSON on stdin or missing ANTHROPIC_API_KEY
"""

import json
import os
import sys

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-5"
MAX_OUTPUT_TOKENS = 512
VALID_TIERS = ("VIP", "Medium", "Low")
EMPTY_USAGE: dict = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}

# Generic system prompt — NO market-specific thresholds, currencies, or locations.
# Per-client rules are injected into the user message at call time so this block
# stays byte-for-byte identical across every tenant, keeping the prompt cache hit
# rate at 100%. The 5-minute cache TTL applies to the hashed token of this exact text.
SYSTEM_PROMPT = """\
You are an expert Real Estate Lead Qualifier. Your task is to analyze prospect data — \
including textual signals AND the attached WhatsApp profile picture — and classify the \
lead into one of three tiers: VIP, Medium, or Low, using the classification rules \
provided in the lead profile below.

[Visual Analysis Guidance]
When a profile picture is provided, look for: professional vs casual setting, attire \
formality, visible luxury markers (cars, watches, premium locations), family/lifestyle \
context, or signs of business activity. Do NOT make assumptions based on appearance, \
gender, or ethnicity — focus strictly on contextual wealth and lifestyle signals visible \
in the frame. If the picture is generic, a logo, or absent, ignore this dimension entirely.

[Required Output Format]
Return a strict JSON object with these exact keys and absolutely no additional text, \
markdown fences, or commentary:
{
  "tier": "VIP" | "Medium" | "Low",
  "confidence": <integer 0-100 reflecting certainty of this classification>,
  "reasoning": "A concise 2-sentence explanation of the classification.",
  "visual_signals": "Brief note on what the profile picture contributed (or 'none' if no useful signal).",
  "sales_strategy": "Actionable advice for the human closer on how to approach this lead."
}\
"""

# Returned when the Claude API fails after one SDK-level retry.
# confidence=0 signals to the server and billing log that no real evaluation occurred.
FALLBACK_RESULT: dict = {
    "tier": "Medium",
    "confidence": 0,
    "reasoning": (
        "Unclassified due to API timeout after retry. "
        "Defaulting to Medium to prevent lead loss."
    ),
    "visual_signals": "none",
    "sales_strategy": (
        "Treat as a standard Medium lead — manual review recommended "
        "before assigning to a closer."
    ),
}


def _build_user_content(lead: dict, tenant=None) -> list:
    """Assemble the multimodal message content list for the API call.

    Per-client classification rules (budget thresholds, currency, VIP locations,
    output language) are injected here into the user message — never into the
    system prompt — so the cached system prompt token is identical for all tenants.
    """
    from tools.db import DEFAULT_TENANT
    cfg = tenant if tenant is not None else DEFAULT_TENANT

    vip_locs = ", ".join(cfg.vip_locations)
    rules = (
        "[Classification Rules — apply these exactly]\n"
        f"- VIP Lead: Target areas ({vip_locs}), "
        f"OR budget >= {cfg.budget_vip_min:,} {cfg.currency}, "
        "OR cash-ready timeline, "
        "OR profile picture shows clear affluence or executive context.\n"
        f"- Medium Lead: Standard residential with payment plan, "
        f"budget {cfg.budget_medium_min:,}–{cfg.budget_vip_min:,} {cfg.currency}, "
        "or 6-month decision timeline.\n"
        f"- Low Lead: Budget < {cfg.budget_medium_min:,} {cfg.currency} with no urgency, "
        "seeking rentals only, VoIP/invalid phone, or unserious engagement.\n"
    )

    lang_note = (
        "\n[Language Instruction]\n"
        "Return all text fields (reasoning, visual_signals, sales_strategy) in Arabic."
        if cfg.output_language == "ar"
        else ""
    )

    content: list = []

    pic_url = (lead.get("wa_profile_picture_url") or "").strip()
    if pic_url:
        content.append({
            "type": "image",
            "source": {"type": "url", "url": pic_url},
        })

    pic_note = (
        "- Profile Picture: [attached — analyze visual context for wealth/lifestyle signals]"
        if pic_url
        else "- Profile Picture: Not available — evaluate on textual signals only."
    )

    text_block = (
        f"{rules}\n"
        "[Lead Profile]\n"
        f"- Lead Name (ManyChat): {lead.get('lead_name') or 'Unknown'}\n"
        f"- WhatsApp Display Name: {lead.get('wa_display_name') or 'Private'}\n"
        f"- WhatsApp Status: {lead.get('wa_status_text') or 'N/A'}\n"
        f"- Preferred Location: {lead.get('location') or 'Not specified'}\n"
        f"- Self-Reported Budget: {lead.get('budget_range') or 'Not specified'}\n"
        f"- Purchase Timeline: {lead.get('timeline') or 'Not specified'}\n"
        f"- Purpose: {lead.get('purpose') or 'Not specified'}\n"
        f"- Phone Carrier/Country: {lead.get('carrier') or 'Unknown'}, "
        f"{lead.get('country_code') or 'Unknown'}\n"
        f"- Phone Validation: {lead.get('phone_valid') or 'unverified'}\n"
        f"{pic_note}"
        f"{lang_note}\n\n"
        "Classify this lead and return ONLY the JSON object."
    )
    content.append({"type": "text", "text": text_block})
    return content


def _parse_claude_json(text: str) -> dict:
    """Extract and validate the JSON object from Claude's response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()

    result = json.loads(text)

    required_keys = ("tier", "confidence", "reasoning", "visual_signals", "sales_strategy")
    missing = [k for k in required_keys if k not in result]
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")

    if result["tier"] not in VALID_TIERS:
        raise ValueError(f"Unexpected tier value: {result['tier']!r}")

    conf = result["confidence"]
    if not isinstance(conf, int) or not (0 <= conf <= 100):
        raise ValueError(
            f"Invalid confidence value: {conf!r} — must be an integer 0–100"
        )

    return result


def evaluate_lead(lead: dict, tenant=None) -> tuple[dict, dict]:
    """Send the enriched lead payload to Claude for qualification.

    Returns tuple[dict, dict]: (evaluation_result, token_usage).
    usage_dict keys: input_tokens, output_tokens, cache_read_tokens.
    On API failure, returns (FALLBACK_RESULT, EMPTY_USAGE).

    tenant: TenantConfig from the DB (HTTP server mode), or None for DEFAULT_TENANT (CLI).
    The SDK retries once automatically on transient errors; on second failure FALLBACK_RESULT
    is returned so every lead is accounted for in the evaluation log.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=api_key, max_retries=1)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Cache the stable, tenant-agnostic system prompt.
                    # First call: ~1.25x write premium. Subsequent calls within
                    # the 5-min TTL: ~0.1x read cost. Effective across all tenants
                    # because per-client rules are in the user message, not here.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_content(lead, tenant),
                }
            ],
        )

        text = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        )
        result = _parse_claude_json(text)
        usage = {
            "input_tokens":      response.usage.input_tokens,
            "output_tokens":     response.usage.output_tokens,
            "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }
        return result, usage

    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"[claude_evaluator] Evaluation failed: {exc}\n")
        return FALLBACK_RESULT, EMPTY_USAGE


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write("[claude_evaluator] ANTHROPIC_API_KEY is not set in .env\n")
        sys.exit(1)

    try:
        lead = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[claude_evaluator] Invalid JSON on stdin: {exc}\n")
        sys.exit(1)

    result, _usage = evaluate_lead(lead)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
