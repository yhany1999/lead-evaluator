#!/usr/bin/env python3
"""
One-shot CLI to provision a new tenant API key.

Run once per client onboarding. The plaintext key is printed once and never stored.

Usage:
    python tools/seed_tenant.py <client_id> <display_name> [options]

Options:
    --budget-vip-min INT       Minimum budget for VIP tier (default: 8000000)
    --budget-medium-min INT    Minimum budget for Medium tier (default: 3000000)
    --currency STR             Currency code (default: EGP)
    --vip-locations STR,...    Comma-separated VIP locations
    --output-language STR      'en' or 'ar' (default: en)

Examples:
    python tools/seed_tenant.py agency-01 "Alpha Realty"
    python tools/seed_tenant.py agency-ae "Dubai Properties" --currency AED --budget-vip-min 2000000 --output-language ar
"""

import argparse
import secrets
import sys

from tools.db import create_tenant, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a new tenant API key")
    parser.add_argument("client_id", help="Unique client identifier (e.g. agency-01)")
    parser.add_argument("name", help="Display name for this client")
    parser.add_argument("--budget-vip-min", type=int, default=8_000_000)
    parser.add_argument("--budget-medium-min", type=int, default=3_000_000)
    parser.add_argument("--currency", default="EGP")
    parser.add_argument(
        "--vip-locations",
        default="North Coast,New Zayed,Gouna,Golden Square,New Cairo",
    )
    parser.add_argument("--output-language", default="en", choices=["en", "ar"])
    args = parser.parse_args()

    api_key = secrets.token_urlsafe(32)
    init_db()

    try:
        create_tenant(
            args.client_id,
            api_key,
            args.name,
            budget_vip_min=args.budget_vip_min,
            budget_medium_min=args.budget_medium_min,
            currency=args.currency,
            vip_locations=args.vip_locations.split(","),
            output_language=args.output_language,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Tenant created: {args.client_id} ({args.name})")
    print(f"API Key: {api_key}")
    print("Save this key — it is hashed in the database and cannot be recovered.")


if __name__ == "__main__":
    main()
