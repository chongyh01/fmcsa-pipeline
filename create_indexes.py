"""Create unique and performance indexes, deduping first where needed."""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")

# 1. carrier_alerts performance index (already created, skipped if exists)
print("carrier_alerts revocation perf index... (already done)", flush=True)

# 2. Dedup authority_history on (dot_number, status, effective_date, revocation_date)
print("Deduplicating authority_history...", flush=True)
cur.execute("""
    DELETE FROM authority_history a
    USING authority_history b
    WHERE a.ctid > b.ctid
      AND a.dot_number = b.dot_number
      AND COALESCE(a.status,'') = COALESCE(b.status,'')
      AND COALESCE(a.effective_date, '1900-01-01') = COALESCE(b.effective_date, '1900-01-01')
      AND COALESCE(a.revocation_date, '1900-01-01') = COALESCE(b.revocation_date, '1900-01-01')
""")
print(f"  Deleted {cur.rowcount:,} duplicate authority_history rows", flush=True)

# 3. Create authority_history unique index
print("Creating authority_history unique index...", flush=True)
cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS authority_hist_unique_idx
    ON authority_history(dot_number,
        COALESCE(status,''),
        COALESCE(effective_date, '1900-01-01'::date),
        COALESCE(revocation_date, '1900-01-01'::date))
""")
print("  Done.", flush=True)

# 4. Dedup insurance on (dot_number, policy_number, effective_date)
print("Checking insurance dedup status...", flush=True)
cur.execute("""
    SELECT COUNT(*) FROM (
        SELECT dot_number, COALESCE(policy_number,''), COALESCE(effective_date, '1900-01-01'::date)
        FROM insurance
        GROUP BY 1,2,3 HAVING COUNT(*) > 1
    ) t
""")
ins_dups = cur.fetchone()[0]
print(f"  Insurance duplicate groups: {ins_dups:,}", flush=True)

if ins_dups > 0:
    print("  Deduplicating insurance...", flush=True)
    cur.execute("""
        DELETE FROM insurance a
        USING insurance b
        WHERE a.ctid > b.ctid
          AND a.dot_number = b.dot_number
          AND COALESCE(a.policy_number,'') = COALESCE(b.policy_number,'')
          AND COALESCE(a.effective_date, '1900-01-01') = COALESCE(b.effective_date, '1900-01-01')
    """)
    print(f"  Deleted {cur.rowcount:,} duplicate insurance rows", flush=True)

# 5. Create insurance unique index
print("Creating insurance unique index...", flush=True)
cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS insurance_unique_idx
    ON insurance(dot_number,
        COALESCE(policy_number,''),
        COALESCE(effective_date, '1900-01-01'::date))
""")
print("  Done.", flush=True)

# 6. Dedup carrier_alerts then index
print("Deduplicating carrier_alerts (INVOLUNTARY_REVOCATION)...", flush=True)
cur.execute("""
    DELETE FROM carrier_alerts a
    USING carrier_alerts b
    WHERE a.ctid > b.ctid
      AND a.event_type = 'INVOLUNTARY_REVOCATION'
      AND b.event_type = 'INVOLUNTARY_REVOCATION'
      AND a.dot_number = b.dot_number
      AND COALESCE(a.event_date::text,'') = COALESCE(b.event_date::text,'')
""")
print(f"  Deleted {cur.rowcount:,} duplicate carrier_alerts rows", flush=True)

cur.close()
conn.close()
print("\nAll done.", flush=True)
