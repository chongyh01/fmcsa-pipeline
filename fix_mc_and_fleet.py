"""
fix_mc_and_fleet.py
===================
Backfill carriers with bad data (mc_number='MC', or fleet=0/0) from Socrata.

WHY THE OLD VERSION WAS SLOW:
  4.17M rows ÷ 500 per batch = 8,346 Socrata API calls × ~5.6s each = ~13 hours.
  The DB writes were already batched (execute_values); the API was the bottleneck.

NEW APPROACH:
  1. Download the FULL Socrata dataset ONCE in pages of 50,000 (~84 API calls)
  2. Build an in-memory dict keyed by dot_number
  3. Fetch all target DOTs from DB in one query
  4. Batch-update the DB in chunks of DB_BATCH rows per round trip

Usage:
  python fix_mc_and_fleet.py           # full run
  python fix_mc_and_fleet.py --test    # timed pilot — reports estimate, does NOT run full job

Env vars required:
  SUPABASE_DB_URL     — psycopg2 connection string
Optional:
  SOCRATA_APP_TOKEN   — higher Socrata rate limits
"""
import os, sys, time, logging, argparse
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("fix_mc_and_fleet.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_URL       = os.getenv("SUPABASE_DB_URL")
SOCRATA      = "https://data.transportation.gov/resource/az4n-8mr2.json"
APP_TOKEN    = os.getenv("SOCRATA_APP_TOKEN", "")
SOCRATA_PAGE = 50_000   # records per Socrata API page
DB_BATCH     = 5_000    # rows per DB round trip
TEST_LIMIT   = 10_000   # rows to DB-update in --test mode

STATUS_MAP = {"A": "ACTIVE", "I": "INACTIVE", "X": "OUT-OF-SERVICE", "N": "NOT AUTHORIZED"}


def get_conn():
    return psycopg2.connect(DB_URL, connect_timeout=30)


def download_socrata():
    """Download full Socrata carrier census; return dict keyed by dot_number."""
    headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
    select = ("dot_number,total_drivers,power_units,status_code,classdef,"
              "docket1prefix,docket1,docket2prefix,docket2,docket3prefix,docket3")
    data = {}
    offset = 0
    page_num = 0
    while True:
        params = {"$limit": SOCRATA_PAGE, "$offset": offset, "$select": select}
        for attempt in range(4):
            try:
                r = requests.get(SOCRATA, params=params, headers=headers, timeout=120)
                r.raise_for_status()
                page = r.json()
                break
            except Exception as e:
                if attempt == 3:
                    log.error(f"  Socrata fetch failed at offset {offset}: {e}")
                    page = []
                    break
                time.sleep(2 ** attempt)
        if not page:
            break
        page_num += 1
        for rec in page:
            dot = rec.get("dot_number")
            if dot:
                data[dot] = rec
        log.info(f"  Socrata page {page_num}: +{len(page):,} records, {len(data):,} total")
        if len(page) < SOCRATA_PAGE:
            break
        offset += SOCRATA_PAGE
    return data


def fetch_target_dots(conn):
    """Return all DOT numbers that need fixing, sorted."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT dot_number FROM carriers
            WHERE mc_number = 'MC'
               OR (total_drivers = 0 AND total_trucks = 0)
            ORDER BY dot_number
        """)
        return [r[0] for r in cur.fetchall()]


def build_row(rec):
    mc = None
    for i in ("1", "2", "3"):
        prefix = (rec.get(f"docket{i}prefix") or "").strip()
        number = (rec.get(f"docket{i}") or "").strip()
        if prefix and number:
            mc = f"{prefix}{number.zfill(6)}"
            break
    status_raw = (rec.get("status_code") or "").strip() or None
    return (
        rec.get("dot_number"),
        int(rec.get("total_drivers") or 0),
        int(rec.get("power_units") or 0),
        STATUS_MAP.get(status_raw, status_raw),
        (rec.get("classdef") or "").strip() or None,
        mc,
    )


def run_db_update(conn, rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            UPDATE carriers SET
                total_drivers = d.total_drivers,
                total_trucks  = d.total_trucks,
                status        = COALESCE(d.status, carriers.status),
                cargo_type    = COALESCE(d.cargo_type, carriers.cargo_type),
                mc_number     = COALESCE(d.mc_number, carriers.mc_number)
            FROM (VALUES %s) AS d(dot_number, total_drivers, total_trucks,
                                  status, cargo_type, mc_number)
            WHERE carriers.dot_number = d.dot_number
        """, rows, template="(%s, %s, %s, %s, %s, %s)", page_size=5000)
    conn.commit()


def batch_update_loop(socrata_data, target_dots, total_dot_count, test_mode=False):
    """Core loop: look up each DOT in socrata_data, batch-update DB."""
    updated = 0
    skipped = 0
    pending = []
    conn = get_conn()
    t_start = time.time()

    for i, dot in enumerate(target_dots):
        rec = socrata_data.get(dot)
        if rec:
            pending.append(build_row(rec))
        else:
            skipped += 1

        if len(pending) >= DB_BATCH:
            try:
                run_db_update(conn, pending)
                updated += len(pending)
            except Exception as e:
                log.error(f"  DB write failed at row {i}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                conn = get_conn()
            pending = []

            elapsed = time.time() - t_start
            rate = (i + 1) / elapsed if elapsed > 0 else 1
            remaining_rows = total_dot_count - (i + 1)
            remaining_sec = remaining_rows / rate
            log.info(f"  {i+1:,}/{total_dot_count:,} — {updated:,} updated — "
                     f"{rate:.0f} rows/sec — ~{remaining_sec/60:.1f}min left")

    if pending:
        try:
            run_db_update(conn, pending)
            updated += len(pending)
        except Exception as e:
            log.error(f"  DB write failed on final flush: {e}")
            conn.rollback()

    conn.close()
    elapsed = time.time() - t_start
    return updated, skipped, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_true",
        help=f"Timed pilot on first {TEST_LIMIT:,} target rows — reports estimate, no full run"
    )
    args = parser.parse_args()

    if not DB_URL:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    # ── Step 1: download full Socrata dataset once ──────────────────────────
    log.info("Step 1: Downloading full Socrata dataset once (replaces 8,000+ per-batch API calls)...")
    t0 = time.time()
    socrata_data = download_socrata()
    socrata_elapsed = time.time() - t0
    log.info(f"  Socrata download done: {len(socrata_data):,} records in {socrata_elapsed:.1f}s")

    # ── Step 2: fetch target DOTs ───────────────────────────────────────────
    log.info("Step 2: Fetching target DOTs from DB...")
    conn = get_conn()
    target_dots = fetch_target_dots(conn)
    conn.close()
    total_dots = len(target_dots)
    log.info(f"  Carriers to fix: {total_dots:,}")

    if not target_dots:
        log.info("Nothing to fix — exiting.")
        return

    # ── Step 3 (test mode): pilot on first TEST_LIMIT rows ─────────────────
    if args.test:
        log.info(f"--test mode: DB-updating first {TEST_LIMIT:,} of {total_dots:,} target rows...")
        pilot_dots = target_dots[:TEST_LIMIT]
        updated, skipped, elapsed = batch_update_loop(
            socrata_data, pilot_dots, TEST_LIMIT, test_mode=True
        )
        db_rate = TEST_LIMIT / elapsed if elapsed > 0 else 1
        full_db_sec = total_dots / db_rate
        total_est_sec = socrata_elapsed + full_db_sec

        log.info("=" * 60)
        log.info(f"TIMING ESTIMATE (from {TEST_LIMIT:,}-row pilot):")
        log.info(f"  Socrata download:       {socrata_elapsed/60:.1f}min  (one-time, already done)")
        log.info(f"  DB update rate:         {db_rate:.0f} rows/sec")
        log.info(f"  Full DB update ({total_dots:,} rows): ~{full_db_sec/60:.1f}min")
        log.info(f"  TOTAL ESTIMATE:         ~{total_est_sec/60:.1f}min")
        log.info("=" * 60)
        log.info("Review the estimate above. If reasonable, re-run WITHOUT --test to start the full job.")
        log.info("NOTE: pilot rows ARE committed to the DB — full run will skip them (already fixed).")
        return

    # ── Step 3 (full run): batch-update all target DOTs ────────────────────
    log.info(f"Step 3: Batch-updating {total_dots:,} carriers in DB (DB_BATCH={DB_BATCH:,})...")
    updated, skipped, elapsed = batch_update_loop(socrata_data, target_dots, total_dots)

    log.info("=" * 60)
    log.info(f"Done. {updated:,} updated, {skipped:,} not found in Socrata, "
             f"total time {elapsed/60:.1f}min.")
    log.info("Next step: run validate_data.py to verify results.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
