"""
Deduplicates insurance table and adds unique index.
Dedup key: (dot_number, policy_number, effective_date) — keep row with latest cancellation_date.
"""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)
conn.autocommit = False

with conn.cursor() as cur:
    # Disable statement timeout for this session so heavy queries can run
    cur.execute("SET statement_timeout = 0")

    cur.execute("SELECT COUNT(*) FROM insurance")
    before = cur.fetchone()[0]
    print(f"Before: {before:,} rows", flush=True)

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,'')
            FROM insurance
            GROUP BY 1,2,3
            HAVING COUNT(*) > 1
        ) t
    """)
    dups = cur.fetchone()[0]
    print(f"Duplicate groups: {dups:,}", flush=True)

    if dups > 0:
        # Keep the row with the latest cancellation_date; if tied, keep highest id
        cur.execute("""
            DELETE FROM insurance
            WHERE id NOT IN (
                SELECT DISTINCT ON (dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,''))
                    id
                FROM insurance
                ORDER BY
                    dot_number,
                    COALESCE(policy_number,''),
                    COALESCE(effective_date::text,''),
                    COALESCE(cancellation_date,'9999-12-31') DESC,
                    id DESC
            )
        """)
        deleted = cur.rowcount
        print(f"Deleted {deleted:,} duplicate rows", flush=True)
        conn.commit()
    else:
        print("No duplicates, skipping delete.", flush=True)
        conn.commit()

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS insurance_unique_idx
        ON insurance(dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,''))
    """)
    conn.commit()
    print("Unique index created/confirmed.", flush=True)

    cur.execute("SELECT COUNT(*) FROM insurance")
    after = cur.fetchone()[0]
    print(f"After: {after:,} rows (removed {before - after:,})", flush=True)

conn.close()
print("Done.", flush=True)
