"""
reimport_revocation_parallel.py
================================
5-worker parallel reimport of revocation_history → carrier_alerts.

Worker 0: deletes INVOLUNTARY_REVOCATION rows, then fetches pages 0,5,10...
Workers 1-4: wait 15s then fetch pages 1-4, 6-9, 11-14...

Usage (run all 5 simultaneously):
  python reimport_revocation_parallel.py --worker 0 --total 5
  python reimport_revocation_parallel.py --worker 1 --total 5
  ...

NOTE: Only deletes INVOLUNTARY_REVOCATION rows — OOS_ORDER rows preserved.
"""
import os, sys, time, logging, argparse, requests
import pandas as pd
import psycopg2, psycopg2.extras
from io import StringIO
from datetime import date
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

parser = argparse.ArgumentParser()
parser.add_argument("--worker",      type=int, required=True)
parser.add_argument("--total",       type=int, default=5)
parser.add_argument("--no-delete",   action="store_true", help="Skip delete of old rows")
args = parser.parse_args()

WID       = args.worker
TOTAL     = args.total
DB_URL    = os.getenv("SUPABASE_DB_URL")
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
DATASET   = "sa6p-acbp"
PAGE_SIZE = 50_000
HEADERS   = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [RevW{WID}] %(message)s",
    handlers=[
        logging.FileHandler(f"reimport_revocation_w{WID}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def sv(v):
    if v is None: return None
    try:
        if pd.isna(v): return None
    except Exception: pass
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit(): s = s[:-2]
    return s or None

def safe_date(v):
    if not v: return None
    try:
        if pd.isna(v): return None
    except Exception: pass
    try:
        return str(pd.to_datetime(v).date())
    except Exception:
        return None

def get_total_rows():
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{DATASET}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30,
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return 1_600_000

def fetch_page(offset):
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{DATASET}.csv",
                params={"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"},
                headers=HEADERS, timeout=120,
            )
            if r.status_code == 400: return pd.DataFrame()
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
    today = date.today()
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number") or r.get("usdot_number"))
        if not dot or dot == "0": continue
        rows.append({
            "dot_number":  dot,
            "event_type":  "INVOLUNTARY_REVOCATION",
            "event_date":  safe_date(
                r.get("order2_effective_date") or r.get("order1_serve_date") or
                r.get("revocation_date") or r.get("action_date")
            ) or str(today),
            "description": sv(r.get("order2_type_desc") or r.get("reason")) or "Authority revoked by FMCSA",
            "source_file": "REVOCATION",
        })
    return rows

def flush(rows):
    if not rows: return
    cols = list(rows[0].keys())
    for attempt in range(4):
        try:
            conn = psycopg2.connect(DB_URL, connect_timeout=30)
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO carrier_alerts ({', '.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    [[r[c] for c in cols] for r in rows], page_size=2000,
                )
            conn.commit()
            conn.close()
            return
        except Exception as e:
            log.warning(f"  flush attempt {attempt+1} failed: {e}")
            try: conn.close()
            except Exception: pass
            if attempt < 3: time.sleep(3 * (attempt + 1))
    log.error("  flush failed after 4 attempts")

def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set"); sys.exit(1)

    if WID == 0 and not args.no_delete:
        log.info("Worker 0: deleting INVOLUNTARY_REVOCATION rows from carrier_alerts...")
        conn = psycopg2.connect(DB_URL, connect_timeout=30)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM carrier_alerts WHERE event_type = 'INVOLUNTARY_REVOCATION'")
            log.info(f"  Deleted {cur.rowcount:,} rows.")
        conn.commit()
        conn.close()
    elif not args.no_delete:
        log.info(f"Worker {WID}: waiting 20s for Worker 0 to clear old rows...")
        time.sleep(20)
    else:
        log.info(f"Worker {WID}: --no-delete mode, starting immediately")

    total_rows = get_total_rows()
    total_pages = (total_rows + PAGE_SIZE - 1) // PAGE_SIZE
    my_pages = list(range(WID, total_pages, TOTAL))
    log.info(f"Total rows: {total_rows:,} → {total_pages} pages | Worker {WID}: {len(my_pages)} pages")

    inserted = 0
    for i, page_num in enumerate(my_pages):
        df = fetch_page(page_num * PAGE_SIZE)
        if df.empty:
            log.info(f"  Page {page_num}: empty — stopping")
            break
        rows = to_rows(df)
        flush(rows)
        inserted += len(rows)
        log.info(f"  Page {page_num} ({i+1}/{len(my_pages)}): {len(rows):,} rows | total: {inserted:,}")

    log.info(f"Worker {WID} DONE: {inserted:,} rows inserted.")

if __name__ == "__main__":
    main()
