"""
Batched dedup for authority_history.
Supabase has a hard 2-minute statement timeout.
Strategy:
  1. Find duplicate IDs in small batches using LIMIT/OFFSET over rows with rn > 1.
  2. Delete those IDs in chunks of 2000.
  3. Loop until no more duplicates found.
  4. Finally create the unique index (also chunked — index creation should be fast on clean table).

Safe to re-run (idempotent).
"""

import os, time, psycopg2
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("SUPABASE_DB_URL")

FETCH_BATCH = 5000   # IDs to collect per SELECT
DELETE_CHUNK = 2000  # IDs to delete per DELETE statement
PAUSE_SECS   = 0.5  # brief pause between statements to avoid hammering

def connect():
    return psycopg2.connect(DB_URL, connect_timeout=30)

def get_dup_ids(conn, limit, offset):
    """Return up to `limit` duplicate IDs (rn > 1) starting at `offset`."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY dot_number,
                                        COALESCE(status, ''),
                                        COALESCE(effective_date::text, ''),
                                        COALESCE(revocation_date::text, '')
                           ORDER BY id
                       ) AS rn
                FROM authority_history
            ) sub
            WHERE rn > 1
            LIMIT %s OFFSET %s
        """, (limit, offset))
        return [row[0] for row in cur.fetchall()]

def delete_ids(conn, ids):
    """Delete a list of IDs in one statement. Keep list small (<= DELETE_CHUNK).
    id column is UUID type, so cast the array explicitly."""
    if not ids:
        return 0
    # Convert UUID objects to strings for the array literal, then cast in SQL
    str_ids = [str(i) for i in ids]
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM authority_history WHERE id = ANY(%s::uuid[])",
            (str_ids,)
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted

def main():
    total_deleted = 0
    round_num = 0

    while True:
        round_num += 1
        print(f"\n--- Round {round_num}: fetching up to {FETCH_BATCH} dup IDs ---", flush=True)

        conn = connect()
        try:
            dup_ids = get_dup_ids(conn, FETCH_BATCH, 0)
        finally:
            conn.close()

        if not dup_ids:
            print("No more duplicates found. Dedup complete!", flush=True)
            break

        print(f"  Found {len(dup_ids)} dup IDs this round. Deleting in chunks of {DELETE_CHUNK}...", flush=True)

        # Delete in sub-chunks to stay well within 2-min timeout
        round_deleted = 0
        for i in range(0, len(dup_ids), DELETE_CHUNK):
            chunk = dup_ids[i : i + DELETE_CHUNK]
            conn = connect()
            try:
                d = delete_ids(conn, chunk)
            finally:
                conn.close()
            round_deleted += d
            total_deleted += d
            print(f"    Chunk {i//DELETE_CHUNK + 1}: deleted {d} rows (total so far: {total_deleted:,})", flush=True)
            time.sleep(PAUSE_SECS)

        print(f"  Round {round_num} done: deleted {round_deleted} rows this round.", flush=True)

        # Safety valve: if we deleted 0 in a round despite finding IDs, something is wrong
        if round_deleted == 0:
            print("WARNING: Found dup IDs but deleted 0 — aborting to avoid infinite loop.", flush=True)
            break

    print(f"\nTotal deleted across all rounds: {total_deleted:,}", flush=True)

    # Now create the unique index
    print("\nCreating unique index (authority_hist_unique_idx) ...", flush=True)
    conn = connect()
    try:
        with conn.cursor() as cur:
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
        print("Unique index created/confirmed successfully.", flush=True)
    except Exception as e:
        print(f"ERROR creating index: {e}", flush=True)
    finally:
        conn.close()

    print("\nAll done.", flush=True)

if __name__ == "__main__":
    main()
