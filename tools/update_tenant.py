#!/usr/bin/env python3
"""
Admin CLI to update tenant configuration fields.

Usage (update fields):
    python tools/update_tenant.py agency-01 --budget-vip-min 10000000
    python tools/update_tenant.py agency-01 --sheets-id 1BxiMVs...
    python tools/update_tenant.py agency-01 --telegram-bot-token 7123:AAF --telegram-chat-id -1001234
    python tools/update_tenant.py agency-01 --wa-notify-url https://... --wa-notify-token tok --wa-notify-to +201...
    python tools/update_tenant.py agency-01 --webhook-url https://crm.example.com/hooks/leads
    python tools/update_tenant.py agency-01 --monthly-quota 2000
    python tools/update_tenant.py agency-01 --suspend
    python tools/update_tenant.py agency-01 --activate

Usage (inspect — no flags):
    python tools/update_tenant.py agency-01
"""

import argparse
import sys

from tools.db import get_tenant_raw, init_db, update_tenant_fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Update tenant configuration")
    parser.add_argument("client_id", help="Tenant client_id (e.g. agency-01)")

    parser.add_argument("--budget-vip-min",    type=int)
    parser.add_argument("--budget-medium-min", type=int)
    parser.add_argument("--currency")
    parser.add_argument("--vip-locations",     help="Comma-separated list of VIP location names")
    parser.add_argument("--output-language",   choices=["en", "ar"])
    parser.add_argument("--monthly-quota",     type=int)
    parser.add_argument("--sheets-id")
    parser.add_argument("--telegram-bot-token")
    parser.add_argument("--telegram-chat-id")
    parser.add_argument("--wa-notify-url")
    parser.add_argument("--wa-notify-token")
    parser.add_argument("--wa-notify-to")
    parser.add_argument("--webhook-url")
    parser.add_argument("--vip-min-confidence", type=int)

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--suspend",  action="store_true", help="Suspend tenant (blocks all API access)")
    group.add_argument("--activate", action="store_true", help="Re-activate a suspended tenant")

    args = parser.parse_args()
    init_db()

    current = get_tenant_raw(args.client_id)
    if current is None:
        print(f"Error: tenant '{args.client_id}' not found.", file=sys.stderr)
        sys.exit(1)

    # Build updates dict from provided flags
    updates: dict = {}
    flag_map = [
        ("budget_vip_min",    args.budget_vip_min),
        ("budget_medium_min", args.budget_medium_min),
        ("currency",          args.currency),
        ("output_language",   args.output_language),
        ("monthly_quota",     args.monthly_quota),
        ("sheets_id",         args.sheets_id),
        ("telegram_bot_token", args.telegram_bot_token),
        ("telegram_chat_id",  args.telegram_chat_id),
        ("wa_notify_url",     args.wa_notify_url),
        ("wa_notify_token",   args.wa_notify_token),
        ("wa_notify_to",      args.wa_notify_to),
        ("webhook_url",       args.webhook_url),
        ("vip_min_confidence", args.vip_min_confidence),
    ]
    for col, val in flag_map:
        if val is not None:
            updates[col] = val

    if args.vip_locations is not None:
        updates["vip_locations"] = args.vip_locations  # stored as comma-separated string

    if args.suspend:
        updates["is_active"] = 0
    if args.activate:
        updates["is_active"] = 1

    # Inspection mode — no flags provided
    if not updates:
        print(f"Config for {args.client_id}:")
        skip = {"api_key_hash"}
        for key, value in current.items():
            if key not in skip:
                print(f"  {key}: {value}")
        return

    # Apply and report changes
    update_tenant_fields(args.client_id, updates)
    for col, new_val in updates.items():
        old_val = current.get(col, "—")
        if col == "is_active":
            print(f"  {args.client_id} {'suspended' if new_val == 0 else 'activated'}.")
        else:
            print(f"  {col}: {old_val!r} → {new_val!r}")


if __name__ == "__main__":
    main()
