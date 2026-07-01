"""
dedup_and_index.py
==================
Post-import cleanup:
1. Deduplicate insurance, authority_history, carrier_alerts (INVOLUNTARY_REVOCATION)
2. Add UNIQUE indexes to prevent future duplicates
3. Add performance indexes for chameleon carrier detection
4. Report row counts before and after

Run AFTER all parallel import workers complete.

Usage:
  python dedup_and_index.py
"""
import os, sys, logging
import psycopg2
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("dedup_and_index.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_URL = os.getenv("SUPABASE_DB_URL")


def run(conn, label, sql, fetch=False):
    log.info(f"  {label}...")
    with conn.cursor() as cur:
        cur.execute(sql)
        if fetch:
            return cur.fetchone()[0]
        affected = cur.rowcount
    conn.commit()
    return affected


def count(conn, table, where=""):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} {where}")
        return cur.fetchone()[0]


def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set"); sys.exit(1)

    conn = psycopg2.connect(DB_URL, connect_timeout=60)
    conn.autocommit = False

    log.info("=" * 60)
    log.info("PRE-DEDUP ROW COUNTS")
    log.info("=" * 60)
    ins_before  = count(conn, "insurance")
    auth_before = count(conn, "authority_history")
    rev_before  = count(conn, "carrier_alerts", "WHERE event_type = 'INVOLUNTARY_REVOCATION'")
    log.info(f"  insurance:          {ins_before:,}")
    log.info(f"  authority_history:  {auth_before:,}")
    log.info(f"  carrier_alerts (revocations): {rev_before:,}")

    # ── 1. Dedup insurance ───────────────────────────────────────
    log.info("\n[1] Deduplicating insurance...")
    run(conn, "Delete insurance duplicates",
        """
        DELETE FROM insurance a
        USING insurance b
        WHERE a.id > b.id
          AND COALESCE(a.dot_number,'')    = COALESCE(b.dot_number,'')
          AND COALESCE(a.policy_number,'') = COALESCE(b.policy_number,'')
          AND COALESCE(a.effective_date::text,'') = COALESCE(b.effective_date::text,'')
        """
    )
    ins_after = count(conn, "insurance")
    log.info(f"  insurance: {ins_before:,} → {ins_after:,} ({ins_before - ins_after:,} removed)")

    run(conn, "Add unique index on insurance",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS insurance_unique_idx
        ON insurance (
            dot_number,
            COALESCE(policy_number, ''),
            COALESCE(effective_date::text, '')
        )
        """
    )

    # ── 2. Dedup authority_history ───────────────────────────────
    log.info("\n[2] Deduplicating authority_history...")
    run(conn, "Delete authority_history duplicates",
        """
        DELETE FROM authority_history a
        USING authority_history b
        WHERE a.id > b.id
          AND COALESCE(a.dot_number,'')      = COALESCE(b.dot_number,'')
          AND COALESCE(a.authority_type,'')  = COALESCE(b.authority_type,'')
          AND COALESCE(a.effective_date::text,'') = COALESCE(b.effective_date::text,'')
          AND COALESCE(a.status,'')          = COALESCE(b.status,'')
        """
    )
    auth_after = count(conn, "authority_history")
    log.info(f"  authority_history: {auth_before:,} → {auth_after:,} ({auth_before - auth_after:,} removed)")

    run(conn, "Add unique index on authority_history",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS authority_hist_unique_idx
        ON authority_history (
            dot_number,
            COALESCE(authority_type, ''),
            COALESCE(effective_date::text, ''),
            COALESCE(status, '')
        )
        """
    )

    # ── 3. Dedup carrier_alerts (revocations only) ───────────────
    log.info("\n[3] Deduplicating carrier_alerts (INVOLUNTARY_REVOCATION)...")
    run(conn, "Delete revocation duplicates",
        """
        DELETE FROM carrier_alerts a
        USING carrier_alerts b
        WHERE a.event_type = 'INVOLUNTARY_REVOCATION'
          AND b.event_type = 'INVOLUNTARY_REVOCATION'
          AND a.id > b.id
          AND COALESCE(a.dot_number,'')  = COALESCE(b.dot_number,'')
          AND COALESCE(a.event_date,'')  = COALESCE(b.event_date,'')
        """
    )
    rev_after = count(conn, "carrier_alerts", "WHERE event_type = 'INVOLUNTARY_REVOCATION'")
    log.info(f"  revocations: {rev_before:,} → {rev_after:,} ({rev_before - rev_after:,} removed)")

    # ── 4. Performance indexes for chameleon carrier detection ────
    log.info("\n[4] Adding performance indexes for chameleon carrier detection...")
    run(conn, "Index carriers.address", "CREATE INDEX IF NOT EXISTS carriers_address_lower_idx ON carriers(LOWER(TRIM(address)))")
    run(conn, "Index carriers.phone",   "CREATE INDEX IF NOT EXISTS carriers_phone_idx ON carriers(phone)")
    run(conn, "Index boc3 company+dot", "CREATE INDEX IF NOT EXISTS boc3_company_dot_idx ON boc3(company_name, dot_number)")

    conn.close()

    log.info("\n" + "=" * 60)
    log.info("DEDUP COMPLETE — FINAL COUNTS")
    log.info("=" * 60)
    log.info(f"  insurance:         {ins_before:,} → {ins_after:,}")
    log.info(f"  authority_history: {auth_before:,} → {auth_after:,}")
    log.info(f"  revocations:       {rev_before:,} → {rev_after:,}")
    log.info("  Unique indexes added on insurance, authority_history")
    log.info("  Performance indexes added on carriers.address, carriers.phone, boc3")


if __name__ == "__main__":
    main()
