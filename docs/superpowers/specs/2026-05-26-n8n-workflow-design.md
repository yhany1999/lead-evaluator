# Spec: n8n Lead Qualification Workflow

**Date:** 2026-05-26
**Project:** Real Estate Lead Qualification SaaS
**Scope:** n8n cloud workflow — ManyChat ingest → Twilio validation → Claude evaluation → tier-based routing

---

## Architecture

Option B: one main workflow handles ingestion, validation, evaluation, and dispatch. Three independent sub-workflows handle tier-specific routing. The main workflow has no routing logic — it only dispatches.

```
ManyChat → [Main Workflow] → Twilio Lookup
                                  │
                          phone invalid/VoIP?
                          YES → [Low Sub-workflow]
                          NO  → POST /evaluate
                                    │
                              Switch on tier
                              ├── VIP    → [VIP Sub-workflow]
                              ├── Medium → [Medium Sub-workflow]
                              └── Low    → [Low Sub-workflow]
```

---

## Main Workflow

**Node 1 — Webhook (Trigger)**
- Method: POST
- Path: `/manychat-lead` (set this URL in ManyChat's HTTP Request action)
- Expected fields: `lead_name`, `phone`, `budget_range`, `location`, `timeline`, `purpose`
- No auth on the webhook itself — ManyChat doesn't support sending custom headers easily; security comes from the non-guessable webhook path

**Node 2 — HTTP Request: Twilio Lookup v2**
- URL: `https://lookups.twilio.com/v2/PhoneNumbers/{{$json.phone}}`
- Query params: `Fields=line_type_intelligence`
- Auth: Basic (Twilio Account SID as username, Auth Token as password)
- Output fields used: `valid`, `line_type_intelligence.type`, `line_type_intelligence.carrier_name`, `calling_country_code`

**Node 3 — IF: Invalid/VoIP Check**
- Condition: `valid == false OR line_type_intelligence.type == "voip"`
- TRUE branch → Execute Sub-workflow: Low (pass full lead payload + reason: "invalid_phone")
- FALSE branch → continue to Node 4

**Node 4 — HTTP Request: POST /evaluate**
- URL: `https://lead-evaluator.onrender.com/evaluate`
- Method: POST
- Header: `X-API-Key: <tenant_api_key>`
- Body (JSON):
  ```json
  {
    "lead_name": "{{$node.Webhook.json.lead_name}}",
    "wa_display_name": "{{$node.Webhook.json.lead_name}}",
    "wa_status_text": "",
    "location": "{{$node.Webhook.json.location}}",
    "budget_range": "{{$node.Webhook.json.budget_range}}",
    "timeline": "{{$node.Webhook.json.timeline}}",
    "purpose": "{{$node.Webhook.json.purpose}}",
    "carrier": "{{$node.Twilio.json.line_type_intelligence.carrier_name}}",
    "country_code": "{{$node.Twilio.json.calling_country_code}}",
    "phone": "{{$node.Webhook.json.phone}}"
  }
  ```
- Error handling: on timeout or 5xx, retry once with 2s delay; on second failure set tier to "Medium" and continue (lead is never dropped)

**Node 5 — Switch: Route by Tier**
- Input: `$node.Evaluate.json.tier`
- Cases: `"VIP"` → VIP sub-workflow, `"Medium"` → Medium sub-workflow, `"Low"` → Low sub-workflow
- Fallback (unexpected value): Medium sub-workflow

---

## Sub-Workflows

### VIP Sub-workflow

Three nodes. All three run — none are conditional.

**Node 1 — WhatsApp Business Cloud**
- Credential: Meta App (Phone Number ID + Access Token)
- To: Sales Director's WhatsApp number (hardcoded per tenant)
- Message template (text):
  ```
  🔴 VIP LEAD ALERT

  Name: {{lead_name}}
  Phone: {{phone}}
  Location: {{location}}
  Budget: {{budget_range}}
  Timeline: {{timeline}}

  Tier: VIP ({{confidence}}% confidence)
  Reasoning: {{reasoning}}

  Sales Strategy: {{sales_strategy}}
  ```

**Node 2 — Gmail (Send Email)**
- Credential: Gmail OAuth
- To: Sales Director email address (hardcoded per tenant)
- Subject: `VIP Lead: {{lead_name}} — {{location}} — {{budget_range}}`
- Body: same content as WhatsApp message, formatted as HTML email

**Node 3 — Google Sheets: Append Row (VIP tab)**
- Spreadsheet: configured per tenant (Spreadsheet ID)
- Sheet name: `VIP`
- Columns: `timestamp`, `lead_name`, `phone`, `location`, `budget_range`, `timeline`, `purpose`, `tier`, `confidence`, `reasoning`, `sales_strategy`

---

### Medium Sub-workflow

**Node 1 — Google Sheets: Append Row (Medium tab)**
- Same spreadsheet as VIP
- Sheet name: `Medium`
- Same columns as VIP sheet

---

### Low Sub-workflow

**Node 1 — Google Sheets: Append Row (Low tab)**
- Same spreadsheet as VIP
- Sheet name: `Low`
- Same columns as VIP sheet, plus `disqualification_reason` (populated from Twilio short-circuit or Claude reasoning)

---

## Google Sheets Structure

One spreadsheet per agency tenant. Three tabs: `VIP`, `Medium`, `Low`.

Column order (all tabs):
| Column | Source |
|--------|--------|
| timestamp | n8n `$now` |
| lead_name | ManyChat payload |
| phone | ManyChat payload |
| location | ManyChat payload |
| budget_range | ManyChat payload |
| timeline | ManyChat payload |
| purpose | ManyChat payload |
| tier | /evaluate response |
| confidence | /evaluate response |
| reasoning | /evaluate response |
| sales_strategy | /evaluate response |
| disqualification_reason | Low tab only — Twilio short-circuit or Claude reasoning |

---

## Credentials Required

| Service | Credential Type | Where to configure |
|---------|----------------|--------------------|
| Twilio | Basic Auth (Account SID + Auth Token) | n8n Credentials → HTTP Basic |
| Render API | HTTP Header (`X-API-Key`) | hardcoded in Node 4 body |
| WhatsApp Business Cloud | Meta App (Phone Number ID + Access Token) | n8n Credentials → WhatsApp Business Cloud |
| Gmail | OAuth2 | n8n Credentials → Gmail |
| Google Sheets | OAuth2 (same Google account) | n8n Credentials → Google Sheets |

---

## Error Handling

| Failure point | Recovery |
|--------------|---------|
| Twilio timeout | Skip lookup, mark `carrier: "unknown"`, proceed to /evaluate |
| /evaluate timeout or 5xx | Retry once (2s delay); on second failure, default tier to Medium, log to Medium sheet |
| Google Sheets write fails | n8n built-in retry (3 attempts); alert via email if all fail |
| WhatsApp send fails | Log error in n8n execution log; email notification still fires independently |

---

## WhatsApp Enrichment (Deferred)

The WhatsApp profile enrichment step (display name + profile picture fetch) is not included in this build — no gateway is configured yet. When a gateway (Whapi.cloud or Wassenger) is selected, add one HTTP Request node between Twilio Lookup and /evaluate, and populate `wa_display_name`, `wa_status_text`, and `wa_profile_picture_url` in the /evaluate payload.

---

## Deployment Notes

- The n8n webhook URL (`/manychat-lead`) goes into ManyChat's "External Request" action as a POST with JSON body
- Sub-workflows are created as separate workflows in n8n and called via the "Execute Workflow" node
- Per-agency configuration (Sales Director phone/email, Google Spreadsheet ID, tenant API key) is hardcoded in each sub-workflow — no dynamic config layer needed at pilot scale
