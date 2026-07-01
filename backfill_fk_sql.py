"""
backfill_fk_sql.py
==================
Fast server-side FK backfill — runs entirely in SQL, no Python round-trips.

The Python version (backfill_inspection_fk-V2.py) fetches data to Python,
matches in memory, then sends UPDATEs back — ~60 batches, ~20 min.

This version does the same logic in two SQL statements that run on the DB
server directly — no data leaves the DB — completes in 1-3 minutes.

Logic (same as V2):
  Case 1 (DOT-ONLY): carrier has exactly 1 inspection row → link ALL its
    violations to that one inspection. Handles ~80% of carriers.
  Case 2 (AMBIGUOUS): carrier has multiple inspections → leave NULL.
    Cannot safely disambiguate without per-violation inspection dates.
"""

import os, sys, time, psycopg2
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Port 5432 direct — avoid PgBouncer timeout on long-running UPDATE
DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")

print("Connecting...")
conn = psycopg2.connect(DB_URL, connect_timeout=30)
conn.autocommit = False
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")   # this UPDATE may take 1-3 min

print("Running server-side FK backfill...")
print("(Linking violations → inspections for DOTs with exactly 1 inspection)")
t0 = time.time()

cur.execute("""
    WITH single_insp AS (
        SELECT dot_number, id
        FROM inspections
        WHERE dot_number IN (
            SELECT dot_number FROM inspections
            GROUP BY dot_number HAVING COUNT(*) = 1
        )
    )
    UPDATE violations v
    SET inspection_id = s.id
    FROM single_insp s
    WHERE v.dot_number = s.dot_number
      AND v.inspection_id IS NULL
""")

updated = cur.rowcount
conn.commit()
elapsed = time.time() - t0
print(f"Done. {updated:,} violations linked in {elapsed:.1f}s")

# Summary
cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
linked = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM violations")
total = cur.fetchone()[0]
pct = linked / total * 100 if total else 0
print(f"violations total: {total:,} | linked: {linked:,} ({pct:.1f}%) | unlinked: {total-linked:,}")
print("(Unlinked = carriers with multiple inspections — cannot safely match without dates)")

cur.close()
conn.close()
