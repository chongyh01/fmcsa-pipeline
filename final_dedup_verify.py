"""
Final verification: confirm 0 duplicates in carrier_alerts and authority_history.
"""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv('SUPABASE_DB_URL'), connect_timeout=30)
conn.autocommit = True

with conn.cursor() as cur:
    cur.execute('SET statement_timeout = 0')

    # --- carrier_alerts ---
    cur.execute("SELECT COUNT(*) FROM carrier_alerts")
    total_ca = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT dot_number, event_type, event_date
            FROM carrier_alerts
            GROUP BY dot_number, event_type, event_date
            HAVING COUNT(*) > 1
        ) sub
    """)
    dups_ca = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM carrier_alerts WHERE event_type = 'INVOLUNTARY_REVOCATION'")
    inv_rev = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM carrier_alerts WHERE event_type = 'OOS_ORDER'")
    oos = cur.fetchone()[0]

    print("=== carrier_alerts ===", flush=True)
    print(f"  Total rows:          {total_ca:,}", flush=True)
    print(f"  INVOLUNTARY_REVOC:   {inv_rev:,}", flush=True)
    print(f"  OOS_ORDER:           {oos:,}", flush=True)
    print(f"  Dup groups remaining: {dups_ca:,}", flush=True)

    # --- authority_history ---
    cur.execute("SELECT COUNT(*) FROM authority_history")
    total_ah = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT dot_number, COALESCE(status,''), COALESCE(effective_date::text,''), COALESCE(revocation_date::text,'')
            FROM authority_history
            GROUP BY 1,2,3,4
            HAVING COUNT(*) > 1
        ) sub
    """)
    dups_ah = cur.fetchone()[0]

    print("\n=== authority_history ===", flush=True)
    print(f"  Total rows:          {total_ah:,}", flush=True)
    print(f"  Dup groups remaining: {dups_ah:,}", flush=True)

    # Check unique index
    cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE tablename = 'authority_history' AND indexname = 'authority_hist_unique_idx'
    """)
    idx = cur.fetchone()
    print(f"  Unique index:        {'EXISTS' if idx else 'MISSING'}", flush=True)

conn.close()
print("\nVerification complete.", flush=True)
