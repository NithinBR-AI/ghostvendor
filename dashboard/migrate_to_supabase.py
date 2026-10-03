"""
One-shot migration: copies all SQLite data (runs, run_events, vendor_results) to Supabase.
Run once from your local machine AFTER setting SUPABASE_URL and SUPABASE_ANON_KEY in .env.

Usage:
    python dashboard/migrate_to_supabase.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so SUPABASE_URL / SUPABASE_ANON_KEY are available
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from supabase import create_client

DB_PATH = Path(__file__).parent / "ghostvendor.db"


def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
        sys.exit(1)

    sb = create_client(url, key)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── runs ────────────────────────────────────────────────
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs").fetchall()]
    print(f"Migrating {len(runs)} runs…")
    for r in runs:
        r.pop("id", None)
        sb.table("runs").upsert(r, on_conflict="run_id").execute()
    print("  runs done")

    # ── run_events ──────────────────────────────────────────
    events = [dict(r) for r in conn.execute("SELECT * FROM run_events").fetchall()]
    print(f"Migrating {len(events)} events…")
    # Insert in batches of 100
    for i in range(0, len(events), 100):
        batch = events[i:i+100]
        # Strip local SQLite id; Supabase uses its own BIGSERIAL
        for e in batch:
            e.pop("id", None)
        sb.table("run_events").insert(batch).execute()
    print("  events done")

    # ── vendor_results ──────────────────────────────────────
    vendors = [dict(r) for r in conn.execute("SELECT * FROM vendor_results").fetchall()]
    print(f"Migrating {len(vendors)} vendor results…")
    for v in vendors:
        v.pop("id", None)
        sb.table("vendor_results").upsert(v, on_conflict="run_id,vendor_name").execute()
    print("  vendor_results done")

    conn.close()
    print("\nMigration complete!")


if __name__ == "__main__":
    main()
