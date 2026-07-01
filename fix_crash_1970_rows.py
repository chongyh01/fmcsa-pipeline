"""
Fix crash 1970-01-01 duplicate rows system-wide.

FMCSA MCMIS exports each crash twice: once with the real date, once with
1970-01-01. Deletes all 1970-01-01 rows in 50K-row ctid chunks — no
correlated subquery, no per-carrier loop, each chunk completes in seconds.
The 39 orphans (no real-date counterpart) are also deleted since they
carry no usable date and no litigation value.
Then adds a unique constraint on (dot_number, report_number).
"""

import os, sys, time
import psycopg2
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URL  = os.getenv("SUPABASE_DB_URL")
CHUNK   = 50_000
TIMEOUT = 60_000  # ms per statement — each 50K chunk should be well under 60s

conn = psycopg2.connect(
    DB_URL, connect_timeout=30,
    options=f"-c statement_timeout={TIMEOUT}"
)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM crashes WHERE crash_date = '1970-01-01'")
total_to_delete = cur.fetchone()[0]
print(f"Rows to delete: {total_to_delete:,}", flush=True)

t0 = time.time()
total_deleted = 0

while True:
    cur.execute(f"""
        DELETE FROM crashes
        WHERE ctid IN (
            SELECT ctid FROM crashes
            WHERE crash_date = '1970-01-01'
            LIMIT {CHUNK}
        )
    """)
    n = cur.rowcount
    if n == 0:
        break
    total_deleted += n
    elapsed = time.time() - t0
    rate = total_deleted / elapsed if elapsed else 0
    remaining = total_to_delete - total_deleted
    eta = remaining / rate / 60 if rate > 0 else 0
    print(f"  {total_deleted:,}/{total_to_delete:,} deleted | "
          f"{rate:.0f} rows/s | ETA ~{eta:.1f}min", flush=True)

# Add unique constraint to prevent future duplicates
print("\nAdding unique constraint (dot_number, report_number)...", flush=True)
try:
    cur.execute("""
        ALTER TABLE crashes
        ADD CONSTRAINT crashes_dot_report_unique
        UNIQUE (dot_number, report_number)
    """)
    print("  Constraint added.", flush=True)
except Exception as e:
    print(f"  Constraint skipped: {e}", flush=True)

cur.execute("SELECT COUNT(*) FROM crashes")
final = cur.fetchone()[0]
cur.close()
conn.close()

elapsed_total = time.time() - t0
print(f"\nDone in {elapsed_total/60:.1f}min. "
      f"Deleted {total_deleted:,} rows. "
      f"Crashes table: {final:,} rows.", flush=True)
