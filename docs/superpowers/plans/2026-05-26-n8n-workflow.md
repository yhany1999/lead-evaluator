# n8n Lead Qualification Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete ManyChat → Twilio → Claude → Google Sheets/WhatsApp/Email routing pipeline in n8n cloud.

**Architecture:** One main workflow handles ingestion, Twilio validation, and Claude evaluation, then dispatches to one of three sub-workflows (VIP, Medium, Low) via Execute Workflow nodes. Sub-workflows handle all output actions independently.

**Tech Stack:** n8n cloud, Twilio Lookup v2, Render-hosted FastAPI (`/evaluate`), WhatsApp Business Cloud API, Gmail, Google Sheets API

**Spec:** `docs/superpowers/specs/2026-05-26-n8n-workflow-design.md`

---

## Task 1: Create Google Spreadsheet

**Files:** None (external resource)

- [ ] **Step 1: Create the spreadsheet**

  Go to [sheets.google.com](https://sheets.google.com) → New spreadsheet → rename it `Lead Evaluator — Cairo Agency`

- [ ] **Step 2: Create the three tabs**

  At the bottom of the spreadsheet, rename "Sheet1" to `VIP`. Add two more tabs: `Medium` and `Low`.

- [ ] **Step 3: Add headers to all three tabs**

  In row 1 of each tab, add these headers in order (A through L):

  ```
  timestamp | lead_name | phone | location | budget_range | timeline | purpose | tier | confidence | reasoning | sales_strategy | disqualification_reason
  ```

  Note: `disqualification_reason` will only be populated in the Low tab — leave it blank for VIP and Medium rows.

- [ ] **Step 4: Copy the Spreadsheet ID**

  From the browser URL: `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`
  Save this ID — you'll need it in Tasks 4, 5, and 6.

---

## Task 2: Configure n8n Credentials

**Files:** None (n8n credential store)

- [ ] **Step 1: Add Twilio credential**

  n8n → Settings → Credentials → New → search "HTTP Basic Auth"
  - Name: `Twilio`
  - User: your Twilio Account SID (from console.twilio.com → Account Info)
  - Password: your Twilio Auth Token

- [ ] **Step 2: Add Google Sheets credential**

  n8n → Settings → Credentials → New → search "Google Sheets"
  - Choose OAuth2
  - Follow the OAuth flow — sign in with the Google account that owns the spreadsheet from Task 1
  - Name it: `Google Sheets — Lead Evaluator`

- [ ] **Step 3: Add Gmail credential**

  n8n → Settings → Credentials → New → search "Gmail"
  - Choose OAuth2
  - Follow the OAuth flow — sign in with the Sales Director's Gmail (or your own for now)
  - Name it: `Gmail — Sales Alerts`

- [ ] **Step 4: Add WhatsApp Business Cloud credential**

  n8n → Settings → Credentials → New → search "WhatsApp Business Cloud"
  - Access Token: your Meta App's permanent system user token
  - Phone Number ID: from Meta Business Suite → WhatsApp → API Setup
  - Name it: `WhatsApp Business — Lead Alerts`

  If you don't have Meta WhatsApp Business API set up yet, skip this step and leave the VIP WhatsApp node disabled in Task 6.

---

## Task 3: Build the Low Sub-workflow

- [ ] **Step 1: Create a new workflow**

  n8n → New Workflow → rename it `Sub: Low Lead`

- [ ] **Step 2: Add Execute Workflow Trigger node**

  Add node → search "Execute Workflow Trigger"
  This is the entry point called by the main workflow. No configuration needed.

- [ ] **Step 3: Add Google Sheets — Append Row node**

  Add node → Google Sheets → "Append or Update Row"
  - Credential: `Google Sheets — Lead Evaluator`
  - Operation: Append Row
  - Document ID: (paste Spreadsheet ID from Task 1, Step 4)
  - Sheet Name: `Low`
  - Column mapping (map each field from the trigger input):
    | Column | Expression |
    |--------|-----------|
    | timestamp | `{{ $now.toISO() }}` |
    | lead_name | `{{ $json.lead_name }}` |
    | phone | `{{ $json.phone }}` |
    | location | `{{ $json.location }}` |
    | budget_range | `{{ $json.budget_range }}` |
    | timeline | `{{ $json.timeline }}` |
    | purpose | `{{ $json.purpose }}` |
    | tier | `{{ $json.tier }}` |
    | confidence | `{{ $json.confidence }}` |
    | reasoning | `{{ $json.reasoning }}` |
    | sales_strategy | `{{ $json.sales_strategy }}` |
    | disqualification_reason | `{{ $json.disqualification_reason }}` |

- [ ] **Step 4: Save and note the workflow ID**

  Save the workflow. From the URL (`/workflow/WORKFLOW_ID`), copy the ID — you'll reference it in the main workflow.

- [ ] **Step 5: Test the sub-workflow in isolation**

  In the Execute Workflow Trigger node, click "Fetch Test Event" and paste this JSON, then click "Execute Workflow":

  ```json
  {
    "lead_name": "Test Low",
    "phone": "+201099999999",
    "location": "Unknown",
    "budget_range": "1M EGP",
    "timeline": "no urgency",
    "purpose": "rental",
    "tier": "Low",
    "confidence": 45,
    "reasoning": "Sub-3M budget with no urgency and rental intent.",
    "sales_strategy": "Add to monthly nurturing list.",
    "disqualification_reason": "Low budget + rental intent"
  }
  ```

  Expected: a new row appears in the `Low` tab of the spreadsheet.

---

## Task 4: Build the Medium Sub-workflow

- [ ] **Step 1: Create a new workflow**

  n8n → New Workflow → rename it `Sub: Medium Lead`

- [ ] **Step 2: Add Execute Workflow Trigger node**

  Add node → "Execute Workflow Trigger". No configuration needed.

- [ ] **Step 3: Add Google Sheets — Append Row node**

  Same configuration as Task 3 Step 3, but:
  - Sheet Name: `Medium`
  - Omit the `disqualification_reason` mapping (leave it blank)

- [ ] **Step 4: Save and note the workflow ID**

  Copy the workflow ID from the URL.

- [ ] **Step 5: Test the sub-workflow in isolation**

  Fetch Test Event → paste this JSON → Execute Workflow:

  ```json
  {
    "lead_name": "Test Medium",
    "phone": "+201088888888",
    "location": "Maadi",
    "budget_range": "4M EGP",
    "timeline": "6 months",
    "purpose": "live-in",
    "tier": "Medium",
    "confidence": 70,
    "reasoning": "Mid-tier budget with payment plan preference and 6-month timeline.",
    "sales_strategy": "Add to standard rotation. Follow up within 3 days."
  }
  ```

  Expected: a new row in the `Medium` tab.

---

## Task 5: Build the VIP Sub-workflow

- [ ] **Step 1: Create a new workflow**

  n8n → New Workflow → rename it `Sub: VIP Lead`

- [ ] **Step 2: Add Execute Workflow Trigger node**

  Add node → "Execute Workflow Trigger". No configuration needed.

- [ ] **Step 3: Add WhatsApp Business Cloud node**

  Add node → "WhatsApp Business Cloud"
  - Credential: `WhatsApp Business — Lead Alerts`
  - Resource: Message
  - Operation: Send
  - To: Sales Director's WhatsApp number in E.164 format (e.g. `+201011112222`)
  - Message Type: Text
  - Text Body:
    ```
    VIP LEAD ALERT

    Name: {{ $json.lead_name }}
    Phone: {{ $json.phone }}
    Location: {{ $json.location }}
    Budget: {{ $json.budget_range }}
    Timeline: {{ $json.timeline }}

    Tier: VIP ({{ $json.confidence }}% confidence)
    Reasoning: {{ $json.reasoning }}

    Sales Strategy: {{ $json.sales_strategy }}
    ```

  If WhatsApp credentials are not ready yet: add the node but toggle it to **disabled** (right-click → Disable). The workflow will still run and skip it.

- [ ] **Step 4: Add Gmail node**

  Add node → "Gmail" → "Send Email"
  - Credential: `Gmail — Sales Alerts`
  - To: Sales Director's email address
  - Subject: `VIP Lead: {{ $json.lead_name }} — {{ $json.location }} — {{ $json.budget_range }}`
  - Email Type: HTML
  - Message:
    ```html
    <h2>VIP Lead Alert</h2>
    <table>
      <tr><td><b>Name</b></td><td>{{ $json.lead_name }}</td></tr>
      <tr><td><b>Phone</b></td><td>{{ $json.phone }}</td></tr>
      <tr><td><b>Location</b></td><td>{{ $json.location }}</td></tr>
      <tr><td><b>Budget</b></td><td>{{ $json.budget_range }}</td></tr>
      <tr><td><b>Timeline</b></td><td>{{ $json.timeline }}</td></tr>
      <tr><td><b>Confidence</b></td><td>{{ $json.confidence }}%</td></tr>
    </table>
    <h3>Reasoning</h3>
    <p>{{ $json.reasoning }}</p>
    <h3>Sales Strategy</h3>
    <p>{{ $json.sales_strategy }}</p>
    ```

- [ ] **Step 5: Add Google Sheets — Append Row node**

  Same configuration as Task 3 Step 3, but:
  - Sheet Name: `VIP`
  - Omit `disqualification_reason`

- [ ] **Step 6: Save and note the workflow ID**

  Copy the workflow ID from the URL.

- [ ] **Step 7: Test the sub-workflow in isolation**

  Fetch Test Event → paste this JSON → Execute Workflow:

  ```json
  {
    "lead_name": "Ahmed Hassan",
    "phone": "+201012345678",
    "location": "New Cairo - Golden Square",
    "budget_range": "10M EGP",
    "timeline": "immediate",
    "purpose": "investment",
    "tier": "VIP",
    "confidence": 92,
    "reasoning": "High-budget target area with immediate investment intent.",
    "sales_strategy": "Contact within the hour. Prepare premium inventory list."
  }
  ```

  Expected: WhatsApp message sent (or skipped if disabled), email received, new row in `VIP` tab.

---

## Task 6: Build the Main Workflow

- [ ] **Step 1: Create a new workflow**

  n8n → New Workflow → rename it `Main: Lead Qualification`

- [ ] **Step 2: Add Webhook node (trigger)**

  Add node → "Webhook"
  - HTTP Method: POST
  - Path: `manychat-lead`
  - Authentication: None
  - Response Mode: "Respond immediately" (don't wait for workflow to finish)

  After saving, copy the **Production webhook URL** — you'll need it for ManyChat in Task 7.

- [ ] **Step 3: Add HTTP Request node — Twilio Lookup**

  Add node → "HTTP Request"
  - Name it: `Twilio Lookup`
  - Method: GET
  - URL: `https://lookups.twilio.com/v2/PhoneNumbers/{{ encodeURIComponent($json.body.phone) }}`
  - Authentication: Generic Credential Type → select `Twilio` (Basic Auth)
  - Query Parameters: add one parameter:
    - Name: `Fields`
    - Value: `line_type_intelligence`
  - On Error: Continue (so Twilio failures don't kill the flow)

- [ ] **Step 4: Add IF node — Invalid/VoIP check**

  Add node → "IF"
  - Name it: `Invalid Phone?`
  - Condition 1: `{{ $json.valid }}` equals `false`
  - Add OR condition
  - Condition 2: `{{ $json.line_type_intelligence.type }}` equals `voip`

  TRUE output → will connect to Low sub-workflow (Step 8)
  FALSE output → will connect to /evaluate (Step 5)

- [ ] **Step 5: Add HTTP Request node — POST /evaluate**

  Add node → "HTTP Request"
  - Name it: `Claude Evaluate`
  - Method: POST
  - URL: `https://lead-evaluator.onrender.com/evaluate`
  - Authentication: None (key goes in headers)
  - Headers: add one header:
    - Name: `X-API-Key`
    - Value: `H6qKZxrsYyA2z0x6ixQaqXjJ5duKB5MMzFi2g9ILe0c`
  - Body Content Type: JSON
  - Body (use "Specify Body" → "Using JSON"):
    ```json
    {
      "lead_name": "{{ $('Webhook').item.json.body.lead_name }}",
      "wa_display_name": "{{ $('Webhook').item.json.body.lead_name }}",
      "wa_status_text": "",
      "location": "{{ $('Webhook').item.json.body.location }}",
      "budget_range": "{{ $('Webhook').item.json.body.budget_range }}",
      "timeline": "{{ $('Webhook').item.json.body.timeline }}",
      "purpose": "{{ $('Webhook').item.json.body.purpose }}",
      "carrier": "{{ $('Twilio Lookup').item.json.line_type_intelligence.carrier_name ?? 'unknown' }}",
      "country_code": "{{ $('Twilio Lookup').item.json.calling_country_code ?? 'EG' }}",
      "phone": "{{ $('Webhook').item.json.body.phone }}"
    }
    ```

- [ ] **Step 6: Add Set node — Merge lead + evaluation data**

  Add node → "Set"
  - Name it: `Merge Payload`
  - Mode: Manual mapping
  - Add these fields:

  | Field | Expression |
  |-------|-----------|
  | lead_name | `{{ $('Webhook').item.json.body.lead_name }}` |
  | phone | `{{ $('Webhook').item.json.body.phone }}` |
  | location | `{{ $('Webhook').item.json.body.location }}` |
  | budget_range | `{{ $('Webhook').item.json.body.budget_range }}` |
  | timeline | `{{ $('Webhook').item.json.body.timeline }}` |
  | purpose | `{{ $('Webhook').item.json.body.purpose }}` |
  | tier | `{{ $('Claude Evaluate').item.json.tier }}` |
  | confidence | `{{ $('Claude Evaluate').item.json.confidence }}` |
  | reasoning | `{{ $('Claude Evaluate').item.json.reasoning }}` |
  | sales_strategy | `{{ $('Claude Evaluate').item.json.sales_strategy }}` |
  | disqualification_reason | `` (empty string) |

  This node sits between "Claude Evaluate" and "Route by Tier" — it's what all three Execute Workflow nodes will receive as input.

- [ ] **Step 7: Add Switch node — Route by tier**

  Add node → "Switch"
  - Name it: `Route by Tier`
  - Mode: Rules
  - Value to switch on: `{{ $json.tier }}`
  - Rule 1: equals `VIP` → output 0
  - Rule 2: equals `Medium` → output 1
  - Rule 3: equals `Low` → output 2
  - Fallback output: output 1 (Medium — leads are never dropped)

- [ ] **Step 8: Add Execute Workflow node — VIP**

  Add node → "Execute Workflow"
  - Name it: `Dispatch VIP`
  - Source: Database
  - Workflow ID: (paste VIP sub-workflow ID from Task 5 Step 6)
  - Pass input data: enabled (passes the full merged payload from Step 6)

- [ ] **Step 9: Add Execute Workflow node — Medium**

  Same as Step 8 but:
  - Name it: `Dispatch Medium`
  - Workflow ID: Medium sub-workflow ID from Task 4 Step 4

- [ ] **Step 10: Add Execute Workflow node — Low**

  Same as Step 8 but:
  - Name it: `Dispatch Low`
  - Workflow ID: Low sub-workflow ID from Task 3 Step 4

  For the Low path coming from the Invalid Phone IF node (TRUE branch), add a Set node before Dispatch Low:
  - Add node → "Set" → name it `Set Low Defaults`
  - Set these fields manually:
    | Field | Value |
    |-------|-------|
    | lead_name | `{{ $('Webhook').item.json.body.lead_name }}` |
    | phone | `{{ $('Webhook').item.json.body.phone }}` |
    | location | `{{ $('Webhook').item.json.body.location }}` |
    | budget_range | `{{ $('Webhook').item.json.body.budget_range }}` |
    | timeline | `{{ $('Webhook').item.json.body.timeline }}` |
    | purpose | `{{ $('Webhook').item.json.body.purpose }}` |
    | tier | `Low` |
    | confidence | `0` |
    | reasoning | `Phone failed validation` |
    | sales_strategy | `Add to monthly nurturing list` |
    | disqualification_reason | `invalid_or_voip_phone` |

- [ ] **Step 11: Connect the nodes**

  Wire in this order:
  ```
  Webhook → Twilio Lookup → Invalid Phone?
                                ├── TRUE  → Set Low Defaults → Dispatch Low
                                └── FALSE → Claude Evaluate → Merge Payload → Route by Tier
                                                                                  ├── VIP    → Dispatch VIP
                                                                                  ├── Medium → Dispatch Medium
                                                                                  └── Low    → Dispatch Low
  ```

- [ ] **Step 12: Activate the workflow**

  Toggle the workflow to **Active** (top-right switch). This makes the Production webhook URL live.

---

## Task 7: End-to-End Test

- [ ] **Step 1: Test a VIP lead**

  From your terminal:
  ```bash
  curl -s -X POST <your-n8n-production-webhook-url> \
    -H "Content-Type: application/json" \
    -d '{
      "lead_name": "Ahmed Hassan",
      "phone": "+201012345678",
      "location": "New Cairo - Golden Square",
      "budget_range": "10M EGP",
      "timeline": "immediate",
      "purpose": "investment"
    }'
  ```

  Expected:
  - Row appears in `VIP` tab of Google Sheet
  - Email received by Sales Director
  - WhatsApp message sent (if enabled)

- [ ] **Step 2: Test a Medium lead**

  ```bash
  curl -s -X POST <your-n8n-production-webhook-url> \
    -H "Content-Type: application/json" \
    -d '{
      "lead_name": "Sara Mohamed",
      "phone": "+201023456789",
      "location": "Maadi",
      "budget_range": "4M EGP",
      "timeline": "6 months",
      "purpose": "live-in"
    }'
  ```

  Expected: Row in `Medium` tab, no alert.

- [ ] **Step 3: Test an invalid phone (short-circuit)**

  ```bash
  curl -s -X POST <your-n8n-production-webhook-url> \
    -H "Content-Type: application/json" \
    -d '{
      "lead_name": "Unknown",
      "phone": "+1555000000",
      "location": "Unknown",
      "budget_range": "unknown",
      "timeline": "unknown",
      "purpose": "unknown"
    }'
  ```

  Expected: Row in `Low` tab with `disqualification_reason: invalid_or_voip_phone`. Twilio + Claude nodes should NOT have fired.

- [ ] **Step 4: Check n8n execution log**

  In n8n → Executions tab, review the last 3 runs. Confirm each node shows green (success) or the correct skip path.

---

## Task 8: Wire ManyChat

- [ ] **Step 1: Open ManyChat automation**

  ManyChat → Automation → find the flow triggered when a lead completes your ad opt-in sequence.

- [ ] **Step 2: Add an "External Request" action**

  At the end of the flow, add action → "External Request"
  - Method: POST
  - URL: (paste the n8n Production webhook URL from Task 6 Step 2)
  - Headers: `Content-Type: application/json`
  - Body (map ManyChat custom fields to the JSON keys):
    ```json
    {
      "lead_name": "{{first name}} {{last name}}",
      "phone": "{{phone}}",
      "location": "{{custom_field: preferred_location}}",
      "budget_range": "{{custom_field: budget_range}}",
      "timeline": "{{custom_field: purchase_timeline}}",
      "purpose": "{{custom_field: purchase_purpose}}"
    }
    ```

  Adjust field names to match your actual ManyChat custom field slugs.

- [ ] **Step 3: Test with a live ManyChat trigger**

  Manually trigger the ManyChat flow on a test contact. Verify the lead appears in the correct Google Sheets tab within ~10 seconds.
