"""
reimport_sms.py
===============
Truncate + reload sms_scores from FMCSA SMS Output dataset (m3ry-qcip).

WHY A SEPARATE SCRIPT:
  fmcsa_import.py's upsert_rows wraps all rows in one transaction — a single
  FK violation (DOT not in carriers table) aborts the entire page. With only
  2 pages covering 8,842 rows, both pages failed and sms_scores has 0 rows.

THIS SCRIPT:
  1. Pre-loads all valid DOT numbers from the carriers table.
  2. Skips any SMS row whose DOT is not in that set.
  3. Inserts in small batches with per-batch error handling.

Env vars required:
  SUPABASE_DB_URL — psycopg2 connection string
Optional:
  SOCRATA_APP_TOKEN — higher rate limits
"""
import os, sys, time, logging
import requests
import pandas as pd
import psycopg2
import psycopg2.extras
from io import StringIO
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("reimport_sms.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_URL     = os.getenv("SUPABASE_DB_URL")
APP_TOKEN  = os.getenv("SOCRATA_APP_TOKEN", "")
DATASET_ID = "m3ry-qcip"
PAGE_SIZE  = 50_000
BATCH_SIZE = 1_000

HEADERS = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

# Score percentile columns: Socrata field → DB column
# NOTE: crash_ind_pct and hazmat_pct do NOT exist in this Socrata dataset (m3ry-qcip).
SCORE_MAP = {
    "unsafe_driv_pct":    "unsafe_driving",
    "hos_driv_pct":       "hours_of_service_compliance",
    "driv_fit_pct":       "driver_fitness",
    "contr_subst_pct":    "controlled_substances_alcohol",
    "veh_maint_pct":      "vehicle_maintenance",
}

# Alert flag columns: Socrata field → DB column
ALERT_MAP = {
    "unsafe_driv_basic_alert":    "unsafe_driving_alert",
    "hos_driv_basic_alert":       "hours_of_service_compliance_alert",
    "driv_fit_basic_alert":       "driver_fitness_alert",
    "contr_subst_basic_alert":    "controlled_substances_alcohol_alert",
    "veh_maint_basic_alert":      "vehicle_maintenance_alert",
}


def sv(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("nan", "none", "") else s


def fetch_valid_dots(conn):
    # Not used — replaced by row-by-row fallback to avoid statement timeout
    return set()


def fetch_all_pages():
    rows_all = []
    offset = 0
    page = 1
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        for attempt in range(4):
            try:
                r = requests.get(
                    f"https://data.transportation.gov/resource/{DATASET_ID}.csv",
                    params=params, headers=HEADERS, timeout=120,
                )
                r.raise_for_status()
                df = pd.read_csv(StringIO(r.text), low_memory=False)
                df.columns = [c.strip().lower() for c in df.columns]
                break
            except Exception as e:
                if attempt == 3:
                    log.error(f"  Failed page {page}: {e}")
                    df = pd.DataFrame()
                    break
                time.sleep(2 ** attempt)
        if df.empty:
            break
        rows_all.append(df)
        log.info(f"  Page {page}: {len(df):,} rows")
        if len(df) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        page += 1
    return pd.concat(rows_all, ignore_index=True) if rows_all else pd.DataFrame()


def build_rows(df, valid_dots, score_date):
    rows = []
    skipped_fk = 0
    cols = set(df.columns)
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        row = {"dot_number": dot, "score_date": score_date}
        for src, dst in SCORE_MAP.items():
            if src not in cols:
                row[dst] = None
                continue
            val = r.get(src)
            try:
                if pd.isna(val):
                    row[dst] = None
                    continue
            except Exception:
                pass
            try:
                row[dst] = float(str(val).strip().rstrip("%"))
            except (ValueError, TypeError):
                row[dst] = None
        for src, dst in ALERT_MAP.items():
            if src not in cols:
                row[dst] = None
                continue
            val = r.get(src)
            try:
                if pd.isna(val):
                    row[dst] = None
                    continue
            except Exception:
                pass
            row[dst] = str(val).strip().upper() in ("Y", "YES", "1", "TRUE")
        rows.append(row)
    return rows, skipped_fk


def insert_single(conn, row):
    cols = list(row.keys())
    vals = [row[c] for c in cols]
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO sms_scores ({', '.join(cols)}) VALUES ({', '.join(['%s']*len(cols))})",
            vals,
        )
    conn.commit()


def insert_batch(conn, rows):
    if not rows:
        return 0, 0
    cols = list(rows[0].keys())
    vals = [[r[c] for c in cols] for r in rows]
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO sms_scores ({', '.join(cols)}) VALUES %s",
                vals, page_size=500,
            )
        conn.commit()
        return len(rows), 0
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        # Fall back to row-by-row to skip only the bad DOTs
        ok = skipped = 0
        for row in rows:
            try:
                insert_single(conn, row)
                ok += 1
            except psycopg2.errors.ForeignKeyViolation:
                conn.rollback()
                skipped += 1
                log.warning(f"  Skipped DOT {row.get('dot_number')} — not in carriers table")
        return ok, skipped
    except Exception as e:
        conn.rollback()
        log.error(f"  Batch insert failed: {e}")
        return 0, 0


def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    conn = psycopg2.connect(DB_URL, connect_timeout=30)

    log.info("Step 1: Truncating sms_scores...")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE sms_scores")
    conn.commit()
    log.info("  Cleared.")

    log.info("Step 2: Downloading SMS dataset from Socrata...")
    from datetime import date
    score_date = date.today()
    df = fetch_all_pages()
    if df.empty:
        log.error("No data fetched — aborting")
        conn.close()
        return
    log.info(f"  Downloaded: {len(df):,} total rows")
    log.info(f"  Columns: {list(df.columns)}")

    log.info("Step 3: Building rows...")
    rows, _ = build_rows(df, set(), score_date)
    log.info(f"  {len(rows):,} rows to insert")

    log.info("Step 4: Inserting in batches (FK violations fall back to row-by-row)...")
    inserted = skipped_total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        ok, skipped = insert_batch(conn, batch)
        inserted += ok
        skipped_total += skipped
        log.info(f"  {inserted:,}/{len(rows):,} inserted, {skipped_total:,} skipped")

    conn.close()
    log.info("=" * 60)
    log.info(f"DONE. {inserted:,} rows inserted, {skipped_total:,} skipped (DOT not in carriers).")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
