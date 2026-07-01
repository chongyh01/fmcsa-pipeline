"""
Deduplicates INVOLUNTARY_REVOCATION rows in carrier_alerts.
Keeps one row per (dot_number, event_date) combination.
Does NOT touch OOS_ORDER rows.
Processes in batches to avoid statement timeout.
"""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

BATCH_SIZE = 5000

def get_conn():
    return psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)

conn = get_conn()
conn.autocommit = False

with conn.cursor() as cur:
    cur.execute("SET statement_timeout = 0")
    cur.execute("SELECT COUNT(*) FROM carrier_alerts WHERE event_type = 'INVOLUNTARY_REVOCATION'")
    before = cur.fetchone()[0]
    print(f"Before: {before:,} INVOLUNTARY_REVOCATION rows", flush=True)

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT dot_number, COALESCE(event_date::text,'')
            FROM carrier_alerts
            WHERE event_type = 'INVOLUNTARY_REVOCATION'
            GROUP BY 1,2
            HAVING COUNT(*) > 1
        ) t
    """)
    dups = cur.fetchone()[0]
    print(f"Duplicate groups: {dups:,}", flush=True)

conn.close()

if dups == 0:
    print("No duplicates found.", flush=True)
else:
    total_deleted = 0
    batch_num = 0

    while True:
        # Fresh connection per batch — avoids pgBouncer connection drops mid-batch
        conn = get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 0")

        # Fetch dot_numbers that have TRUE duplicate (dot_number, event_date) pairs
        cur.execute("""
            SELECT DISTINCT dot_number
            FROM carrier_alerts
            WHERE event_type = 'INVOLUNTARY_REVOCATION'
            GROUP BY dot_number, COALESCE(event_date::text,'')
            HAVING COUNT(*) > 1
            LIMIT %s
        """, (BATCH_SIZE,))
        dot_numbers = [r[0] for r in cur.fetchall()]

        if not dot_numbers:
            cur.close()
            conn.close()
            break

        # Single batch DELETE — keep MIN(ctid) per (dot_number, event_date) group
        cur.execute("""
            DELETE FROM carrier_alerts
            WHERE event_type = 'INVOLUNTARY_REVOCATION'
              AND dot_number = ANY(%s)
              AND ctid NOT IN (
                  SELECT MIN(ctid)
                  FROM carrier_alerts
                  WHERE event_type = 'INVOLUNTARY_REVOCATION'
                    AND dot_number = ANY(%s)
                  GROUP BY dot_number, COALESCE(event_date::text,'')
              )
        """, (dot_numbers, dot_numbers))

        deleted = cur.rowcount
        total_deleted += deleted
        batch_num += 1
        print(f"  Batch {batch_num}: deleted {deleted:,} rows (total: {total_deleted:,})", flush=True)
        cur.close()
        conn.close()

    print(f"\nTotal deleted: {total_deleted:,} duplicate rows", flush=True)

conn = get_conn()
with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM carrier_alerts WHERE event_type = 'INVOLUNTARY_REVOCATION'")
    after = cur.fetchone()[0]
    print(f"After:  {after:,} INVOLUNTARY_REVOCATION rows", flush=True)

    cur.execute("SELECT COUNT(*) FROM carrier_alerts WHERE event_type = 'OOS_ORDER'")
    oos = cur.fetchone()[0]
    print(f"OOS_ORDER rows (untouched): {oos:,}", flush=True)
conn.close()
print("Done.", flush=True)
