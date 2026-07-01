"""
reimport_authority_parallel.py
===============================
Parallel reimport of authority_history using N worker processes.
Each worker fetches a non-overlapping stripe of pages from Socrata.

Usage:
  python reimport_authority_parallel.py --worker 0 --total 5
  python reimport_authority_parallel.py --worker 1 --total 5
  ... (run all 5 simultaneously in separate terminals / background jobs)

Worker 0 truncates the table before fetching. Workers 1-4 wait 10s then start.

Env vars required:
  SUPABASE_DB_URL
Optional:
  SOCRATA_APP_TOKEN
"""
import os, sys, time, logging, argparse, requests
import pandas as pd
import psycopg2
import psycopg2.extras
from io import StringIO
from dotenv import load_dotenv
from datetime import date

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

parser = argparse.ArgumentParser()
parser.add_argument("--worker", type=int, required=True, help="Worker ID (0-based)")
parser.add_argument("--total",  type=int, default=5,    help="Total number of workers")
args = parser.parse_args()

WORKER_ID    = args.worker
TOTAL        = args.total
DB_URL       = os.getenv("SUPABASE_DB_URL")
APP_TOKEN    = os.getenv("SOCRATA_APP_TOKEN", "")
DATASET_ID   = "9mw4-x3tu"
PAGE_SIZE    = 50_000
HEADERS      = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [W{WORKER_ID}] %(message)s",
    handlers=[
        logging.FileHandler(f"reimport_authority_w{WORKER_ID}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def sv(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None


def safe_date(v):
    if not v:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return str(pd.to_datetime(v).date())
    except Exception:
        return None


def get_total_rows():
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{DATASET_ID}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30,
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return 5_000_000


def fetch_page(offset):
    params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{DATASET_ID}.csv",
                params=params, headers=HEADERS, timeout=120,
            )
            if r.status_code == 400:
                return pd.DataFrame()
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            return df
        except Exception as e:
            if attempt == 4:
                log.warning(f"  Failed offset {offset}: {e}")
                return pd.DataFrame()
            time.sleep(2 ** attempt)
    return pd.DataFrame()


def to_rows(df):
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("usdot_number") or r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":      dot,
            "authority_type":  sv(r.get("mod_col_1") or r.get("authority_type")),
            "status":          sv(r.get("original_action_desc") or r.get("status")),
            "effective_date":  safe_date(r.get("orig_served_date") or r.get("effective_date")),
            "revocation_date": safe_date(r.get("disp_served_date") or r.get("revocation_date")),
            "reason":          sv(r.get("disp_action_desc") or r.get("reason")),
        })
    return rows


def flush(conn, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO authority_history ({', '.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
            [[r[c] for c in cols] for r in rows],
            page_size=2000,
        )
    conn.commit()


def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    # Worker 0 truncates the table; others wait 15s for truncate to complete
    if WORKER_ID == 0:
        log.info("Worker 0: truncating authority_history...")
        conn = psycopg2.connect(DB_URL, connect_timeout=30)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE authority_history")
        conn.commit()
        conn.close()
        log.info("  Truncated.")
    else:
        log.info(f"Worker {WORKER_ID}: waiting 15s for Worker 0 to truncate...")
        time.sleep(15)

    log.info(f"Worker {WORKER_ID}: querying total rows...")
    total_rows = get_total_rows()
    total_pages = (total_rows + PAGE_SIZE - 1) // PAGE_SIZE
    log.info(f"  Total rows: {total_rows:,} → {total_pages} pages")

    # Assign pages: worker N gets pages N, N+total, N+2*total, ...
    my_pages = list(range(WORKER_ID, total_pages, TOTAL))
    log.info(f"  Worker {WORKER_ID} assigned {len(my_pages)} pages: {my_pages[:5]}{'...' if len(my_pages) > 5 else ''}")

    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    inserted = 0
    for i, page_num in enumerate(my_pages):
        offset = page_num * PAGE_SIZE
        df = fetch_page(offset)
        if df.empty:
            log.info(f"  Page {page_num} (offset {offset:,}): empty — done")
            break
        rows = to_rows(df)
        flush(conn, rows)
        inserted += len(rows)
        log.info(f"  Page {page_num} ({i+1}/{len(my_pages)}): {len(rows):,} rows | total inserted: {inserted:,}")

    conn.close()
    log.info(f"Worker {WORKER_ID} DONE: {inserted:,} rows inserted.")


if __name__ == "__main__":
    main()
