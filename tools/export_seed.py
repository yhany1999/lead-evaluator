#!/usr/bin/env python3
"""
Export current tenant configs as a TENANT_SEED JSON env var value.

Usage:
    python tools/export_seed.py

Copy the output and set it as TENANT_SEED in Render environment variables.
The exported JSON includes api_key_hash so existing API keys remain valid
after redeploy — no need to re-issue keys to clients.
"""

import json
import sys

from tools.db import init_db, get_conn

SKIP = {"created_at"}


def main() -> None:
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tenants").fetchall()

    if not rows:
        print("No tenants found in database.", file=sys.stderr)
        sys.exit(1)

    tenants = []
    for row in rows:
        t = {k: row[k] for k in row.keys() if k not in SKIP}
        tenants.append(t)

    print(json.dumps(tenants, indent=2))


if __name__ == "__main__":
    main()
