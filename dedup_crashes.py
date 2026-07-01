"""
Remove duplicate crash rows.
Uses dot_number index + ctid comparison — no full table scan.
Runs 500 targeted DELETEs per transaction to stay under statement timeout.
"""

import os, sys, psycopg2
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
conn.autocommit = False
cur = conn.cursor()

print("Finding duplicate groups...", flush=True)
cur.execute("""
    SELECT dot_number, crash_date, report_number
    FROM crashes
    GROUP BY dot_number, crash_date, report_number
    HAVING COUNT(*) > 1
""")
dup_groups = cur.fetchall()
conn.commit()
print(f"Found {len(dup_groups):,} duplicate groups", flush=True)

if not dup_groups:
    print("No duplicates. Done.")
    cur.close(); conn.close()
    sys.exit(0)

BATCH = 500
total_deleted = 0

for i in range(0, len(dup_groups), BATCH):
    chunk = dup_groups[i : i + BATCH]

    # Each DELETE hits dot_number index — scans only 2-3 rows per group
    for dot, crash_date, report_num in chunk:
        cur.execute("""
            DELETE FROM crashes a
            WHERE a.dot_number = %s
              AND a.crash_date  = %s
              AND a.report_number IS NOT DISTINCT FROM %s
              AND EXISTS (
                  SELECT 1 FROM crashes b
                  WHERE b.dot_number = a.dot_number
                    AND b.crash_date  = a.crash_date
                    AND b.report_number IS NOT DISTINCT FROM a.report_number
                    AND b.ctid < a.ctid
              )
        """, (dot, crash_date, report_num))
        total_deleted += cur.rowcount

    conn.commit()  # one commit per 500 groups

    done = min(i + BATCH, len(dup_groups))
    print(f"  {done:,}/{len(dup_groups):,} groups — {total_deleted:,} rows deleted", flush=True)

cur.execute("SELECT COUNT(*) FROM crashes")
final = cur.fetchone()[0]
conn.commit()
print(f"DONE. Deleted {total_deleted:,} duplicates. Final crash count: {final:,}", flush=True)

cur.close()
conn.close()
