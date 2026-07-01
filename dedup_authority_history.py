"""
Deduplicates authority_history and adds a unique index to prevent future duplicates.
Safe to run multiple times (idempotent).

Uses a CTE-based DELETE to avoid statement timeouts on Supabase.
The dup-count query is skipped — we go straight to DELETE and rely on rowcount.
"""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)
conn.autocommit = False

with conn.cursor() as cur:
    # Raise statement timeout for this session only (default is often 30s on Supabase)
    cur.execute("SET statement_timeout = '600000'")   # 10 minutes

    # Count before
    cur.execute("SELECT COUNT(*) FROM authority_history")
    before = cur.fetchone()[0]
    print(f"Before dedup: {before:,} rows", flush=True)

    # Delete duplicates — keep the row with the lowest id for each unique key group.
    # Uses a CTE (faster than NOT IN on large tables).
    print("Running dedup DELETE (may take a few minutes) ...", flush=True)
    cur.execute("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY dot_number,
                                    COALESCE(status, ''),
                                    COALESCE(effective_date::text, ''),
                                    COALESCE(revocation_date::text, '')
                       ORDER BY id
                   ) AS rn
            FROM authority_history
        )
        DELETE FROM authority_history
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """)
    deleted = cur.rowcount
    print(f"Deleted {deleted:,} duplicate rows", flush=True)
    conn.commit()

    # Add unique index (idempotent — IF NOT EXISTS)
    print("Creating unique index ...", flush=True)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS authority_hist_unique_idx
        ON authority_history(
            dot_number,
            COALESCE(status, ''),
            COALESCE(effective_date::text, ''),
            COALESCE(revocation_date::text, '')
        )
    """)
    conn.commit()
    print("Unique index created/confirmed.", flush=True)

    # Count after
    cur.execute("SELECT COUNT(*) FROM authority_history")
    after = cur.fetchone()[0]
    print(f"After dedup: {after:,} rows (removed {before - after:,})", flush=True)

conn.close()
print("Done.", flush=True)
