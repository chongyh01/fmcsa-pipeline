"""
Create a unique index on authority_history to prevent future duplicates.
Uses NULLS NOT DISTINCT (PostgreSQL 15+) to handle NULL columns properly.
Falls back to a plain unique index if NULLS NOT DISTINCT is not supported.
"""
import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv('SUPABASE_DB_URL'), connect_timeout=30)
conn.autocommit = False

with conn.cursor() as cur:
    cur.execute("SET statement_timeout = 0")

    # Check PostgreSQL version
    cur.execute("SELECT version()")
    version = cur.fetchone()[0]
    print(f"PostgreSQL version: {version}", flush=True)

    # Drop old index if it exists (from failed prior attempt)
    cur.execute("DROP INDEX IF EXISTS authority_hist_unique_idx")
    conn.commit()
    print("Dropped old index (if existed).", flush=True)

    # Try NULLS NOT DISTINCT first (PostgreSQL 15+)
    try:
        print("Attempting NULLS NOT DISTINCT unique index...", flush=True)
        cur.execute("""
            CREATE UNIQUE INDEX authority_hist_unique_idx
            ON authority_history(dot_number, status, effective_date, revocation_date)
            NULLS NOT DISTINCT
        """)
        conn.commit()
        print("Unique index (NULLS NOT DISTINCT) created successfully.", flush=True)
    except Exception as e:
        conn.rollback()
        print(f"NULLS NOT DISTINCT failed ({e}), trying partial index approach...", flush=True)
        # Fallback: just index the columns directly (NULLs treated as distinct by default)
        cur.execute("""
            CREATE UNIQUE INDEX authority_hist_unique_idx
            ON authority_history(dot_number, COALESCE(status, 'NULL_STATUS'), effective_date, revocation_date)
        """)
        conn.commit()
        print("Fallback partial index created.", flush=True)

    # Verify index exists
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE tablename = 'authority_history' AND indexname = 'authority_hist_unique_idx'
    """)
    row = cur.fetchone()
    if row:
        print(f"Index confirmed: {row[0]}", flush=True)
    else:
        print("WARNING: Index not found after creation!", flush=True)

conn.close()
print("Done.", flush=True)
