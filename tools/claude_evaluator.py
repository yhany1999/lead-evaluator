#!/usr/bin/env python3
"""
Real estate lead evaluation via Claude Sonnet 4.5 Vision.

Reads a JSON lead payload from stdin.
Writes a classification JSON object to stdout.
Writes diagnostic messages to stderr.

Input fields (all strings unless noted):
    lead_name             - prospect name from ManyChat
    wa_display_name       - WhatsApp display name (or "Private" if unavailable)
    wa_status_text        - WhatsApp "About" text (or empty string)
    location              - preferred property location / project
    budget_range          - self-reported budget
    timeline              - purchase timeline
    purpose               - "live-in" or "investment"
    carrier               - phone carrier from Twilio Lookup
    country_code          - ISO country code from Twilio Lookup
    phone_valid           - "true" | "false" | "unverified"
    wa_profile_picture_url - publicly accessible image URL (or omit / empty string)

Output JSON:
    tier           - "VIP" | "Medium" | "Low"
    reasoning      - 2-sentence classification explanation
    visual_signals - profile picture contribution (or "none")
    sales_strategy - actionable advice for the closer

Usage:
    echo '{...}' | python tools/claude_evaluator.py
    python tools/claude_evaluator.py < lead_payload.json

Exit codes:
    0  success (including API-fallback result)
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

# Stable system prompt — marked for caching on every request.
# Sonnet 4.5 minimum cacheable prefix is 1024 tokens; this prompt exceeds that.
SYSTEM_PROMPT = """\
You are an expert Real Estate Lead Qualifier operating in the Egyptian and Gulf market. \
Your task is to analyze the following prospect data — including textual signals AND the \
attached WhatsApp profile picture — and classify the lead into one of three tiers: \
VIP, Medium, or Low.

[Classification Rules]
- VIP Lead: High-budget target areas (North Coast, New Zayed, Gouna, \
Golden Square/New Cairo), OR self-reported budget above 8M EGP, OR cash-ready timeline, \
OR visual signals from profile picture indicating affluence/executive status \
(luxury setting, professional attire, business context).
- Medium Lead: Standard residential apartments/townhouses with payment-plan preference, \
mid-tier self-reported budget (3M–8M EGP), or 6-month decision timeline.
- Low Lead: Invalid phone (already filtered at Phase 2A), strictly seeking cheap rentals, \
sub-3M EGP budget with no urgency, or aggressive/unserious behavior in transcripts.

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
  "reasoning": "A concise 2-sentence explanation of the classification.",
  "visual_signals": "Brief note on what the profile picture contributed (or 'none' if no useful signal).",
  "sales_strategy": "Actionable advice for the human closer on how to approach this lead based on their full profile."
}\
"""

# Returned when the Claude API fails after one SDK-level retry (per workflow Edge Case C).
FALLBACK_RESULT: dict = {
    "tier": "Medium",
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


def _build_user_content(lead: dict) -> list:
    """Assemble the multimodal message content list for the API call."""
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
        f"{pic_note}\n\n"
        "Classify this lead and return ONLY the JSON object."
    )
    content.append({"type": "text", "text": text_block})
    return content


def _parse_claude_json(text: str) -> dict:
    """
    Extract and validate the JSON object from Claude's response.
    Handles accidental markdown code fences gracefully.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()

    result = json.loads(text)

    required_keys = ("tier", "reasoning", "visual_signals", "sales_strategy")
    missing = [k for k in required_keys if k not in result]
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")

    if result["tier"] not in VALID_TIERS:
        raise ValueError(f"Unexpected tier value: {result['tier']!r}")

    return result


def evaluate_lead(lead: dict) -> dict:
    """
    Send the enriched lead payload to Claude for qualification.

    The SDK is configured with max_retries=1 so it retries once automatically
    on transient errors (matching workflow Edge Case C). If that retry also
    fails, we return FALLBACK_RESULT to keep every lead accounted for.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(
        api_key=api_key,
        max_retries=1,
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Cache the stable system prompt across all lead evaluations.
                    # First call pays the write premium (~1.25×); subsequent calls
                    # within the 5-minute TTL window pay ~0.1× — significant savings
                    # under high lead volume.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _build_user_content(lead),
                }
            ],
        )

        text = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        )
        return _parse_claude_json(text)

    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.APIStatusError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"[claude_evaluator] Evaluation failed: {exc}\n")
        return FALLBACK_RESULT


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

    result = evaluate_lead(lead)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
