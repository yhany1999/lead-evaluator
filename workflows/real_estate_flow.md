<br>

# WORKFLOW: Inbound AI Lead Generation & Qualification System (Real Estate)

## 1. Objective & Scope

The objective of this system is to capture inbound leads from social media advertisements (Meta/TikTok), enrich their demographic profiles, qualify their buying intent using LLM reasoning, and route high-value prospects directly to the sales team's CRM.

* **Target Market:** Real Estate Agencies (Egypt / Arab Region).
* **Core Metric:** Minimize response time to < 3 seconds, maximize sales team efficiency by filtering out unqualified leads, and secure zero operational friction.

---

## 2. System Architecture & Tech Stack

The infrastructure relies strictly on deterministic data routing combined with AI-agentic evaluation, eliminating heavy custom coding:

* **Capture Layer:** ManyChat (Meta API Integration)
* **Orchestration & Routing Layer:** cloud n8n
* **Enrichment Layer:** Twilio Lookup (phone validation) + WhatsApp Business API (identity & visual signals)
* **Reasoning Layer:** Anthropic Claude API with Vision (lead scoring, profile picture analysis, executive summarization)
* **Storage/CRM Layer:** Google Sheets / Client CRM

---

## 3. Step-by-Step Execution Protocol

```
[Meta/TikTok Ads] ──> [ManyChat] ──> [n8n Webhook] ──> [Twilio Lookup]
                                                              │
                                                              ▼
                                                    [WhatsApp Profile Fetch]
                                                              │
[Sales Team Alert] <── [n8n Router] <── [Claude Evaluation] <─┘
```

### Phase 1: Inbound Ingestion (ManyChat ──> n8n)

* **Input:** Prospect clicks an ad or comments, triggering a DM automation.
* **Data Captured:** First Name, Last Name, Phone Number, WhatsApp Opt-in, Preferred Location/Project, Budget Range, Purchase Timeline, Purpose (live-in / investment).
* **Execution:** ManyChat triggers an external HTTP Request (Webhook) sending a JSON payload to the cloud **n8n** webhook node.

### Phase 2: Autonomous Identity Enrichment (n8n ──> Twilio + WhatsApp)

**Step 2A: Phone Validation (n8n ──> Twilio Lookup API)**

* **Input:** Raw phone number from ManyChat payload.
* **Execution:** n8n calls the Twilio Lookup v2 endpoint passing the phone number with country context (EG/SA/AE/KW).
* **Twilio Actions:**
  * Validate E.164 format and confirm the number is real and active.
  * Identify line type (mobile / landline / VoIP).
  * Return carrier name and country of registration.
* **Output:** Enriched JSON containing `phone_valid`, `line_type`, `carrier`, `country_code`.
* **Branching Logic:**
  * If `phone_valid == false` OR `line_type == "voip"` → short-circuit the flow, route directly to **Low Tier** without burning further API credits.
  * Otherwise → proceed to Step 2B.

**Step 2B: WhatsApp Profile Enrichment (n8n ──> WhatsApp Business API)**

* **Input:** Validated mobile number from Step 2A.
* **Execution:** n8n calls the WhatsApp Business API (via the existing ManyChat integration or a third-party gateway such as Whapi.cloud / Wassenger).
* **WhatsApp Actions:**
  * Fetch the lead's public WhatsApp display name (often the lead's full real name).
  * Fetch profile picture URL (publicly visible per WhatsApp privacy settings).
  * Fetch "About" status text if available.
* **Output:** Enriched JSON payload containing `wa_display_name`, `wa_profile_picture_url`, `wa_status_text`.
* **Privacy Note:** All enrichment data is fetched only after the lead has explicitly opted into WhatsApp contact via ManyChat in Phase 1, ensuring GDPR/MENA data-protection compliance.

### Phase 3: Cognitive Qualification (n8n ──> Claude API with Vision)

* **Input:** Combined enriched payload — ManyChat self-reported data + Twilio validation + WhatsApp profile data + profile picture URL.
* **Model:** Claude Sonnet 4.5 (vision-enabled) for multimodal analysis of profile pictures alongside text signals.
* **Deterministic System Prompt for Claude:**

```text
You are an expert Real Estate Lead Qualifier operating in the Egyptian and Gulf market. Your task is to analyze the following prospect data — including textual signals AND the attached WhatsApp profile picture — and classify the lead into one of three tiers: VIP, Medium, or Low.

[Classification Rules]
- VIP Lead: High-budget target areas (North Coast, New Zayed, Gouna, Golden Square/New Cairo), OR self-reported budget above 8M EGP, OR cash-ready timeline, OR visual signals from profile picture indicating affluence/executive status (luxury setting, professional attire, business context).
- Medium Lead: Standard residential apartments/townhouses with payment-plan preference, mid-tier self-reported budget (3M–8M EGP), or 6-month decision timeline.
- Low Lead: Invalid phone (already filtered at Phase 2A), strictly seeking cheap rentals, sub-3M EGP budget with no urgency, or aggressive/unserious behavior in transcripts.

[Input Variables]
- Lead Name (ManyChat): {{lead_name}}
- WhatsApp Display Name: {{wa_display_name}}
- WhatsApp Status: {{wa_status_text}}
- Preferred Location: {{location}}
- Self-Reported Budget: {{budget_range}}
- Purchase Timeline: {{timeline}}
- Purpose: {{purpose}}
- Phone Carrier/Country: {{carrier}}, {{country_code}}
- Profile Picture: [attached image — analyze visual context for status signals]

[Visual Analysis Guidance]
When a profile picture is provided, look for: professional vs casual setting, attire formality, visible luxury markers (cars, watches, premium locations), family/lifestyle context, or signs of business activity. Do NOT make assumptions based on appearance, gender, or ethnicity — focus strictly on contextual wealth and lifestyle signals visible in the frame. If the picture is generic, a logo, or absent, ignore this dimension entirely.

[Required Output Format]
Return a strict JSON object with these exact keys:
{
  "tier": "VIP" | "Medium" | "Low",
  "reasoning": "A concise 2-sentence explanation of the classification.",
  "visual_signals": "Brief note on what the profile picture contributed (or 'none' if no useful signal).",
  "sales_strategy": "Actionable advice for the human closer on how to approach this lead based on their full profile."
}
```

### Phase 4: Intelligent Routing & Hand-off (n8n ──> CRM / WhatsApp)

* **Execution:** n8n reads the JSON response from Claude and runs a conditional router.
* **Conditional Paths:**
  * **If Tier == "VIP":** n8n instantly sends a high-priority push alert via WhatsApp/Slack API to the Sales Director with the complete Claude profile breakdown, and auto-injects the lead into the CRM under the "Hot Deals" pipeline.
  * **If Tier == "Medium":** n8n logs the lead into the standard sales pipeline for general rotation.
  * **If Tier == "Low":** n8n logs the lead to a low-priority nurturing sheet for automated monthly email campaigns.

---

## 4. Edge Cases & Error Recovery (Deterministic Guardrails)

As per `CLAUDE.md`, execution failures must be handled gracefully without breaking the system loop:

* **Edge Case A: Twilio Lookup fails or times out.**
  * *Recovery:* n8n skips validation, marks `phone_valid: "unverified"`, and proceeds to WhatsApp enrichment. Claude is instructed to weight self-reported data more heavily in absence of phone verification.

* **Edge Case B: WhatsApp profile is private or returns no display name / picture.**
  * *Recovery:* n8n passes fallback values `wa_display_name: "Private"` and omits the image attachment from the Claude call. Claude evaluates strictly on ManyChat self-reported data (location, budget, timeline, purpose).

* **Edge Case C: Claude API Timeout.**
  * *Recovery:* n8n catches the error, retries once with exponential backoff, and if it fails again, routes the lead as "Unclassified - Medium" to ensure the prospect is never dropped or ignored.

* **Edge Case D: Invalid / VoIP phone number detected at Phase 2A.**
  * *Recovery:* Short-circuit to Low Tier immediately. Log to nurturing sheet. Skip WhatsApp + Claude calls entirely to preserve credits.

<br>
