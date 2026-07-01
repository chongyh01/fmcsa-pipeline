"""
reimport_insurance_parallel.py
================================
5-worker parallel reimport of insurance table:
  - InsHist (6sqe-dvqs): ~7.4M rows, ~150 pages
  - ActPendInsur (ypjt-5ydn): ~500K rows, ~10 pages

Worker 0: truncates insurance table, then fetches pages 0,5,10...
Workers 1-4: wait 15s then fetch pages 1-4, 6-9, 11-14...

After all InsHist workers complete, each worker also handles
its stripe of ActPendInsur pages (needs docket→DOT mapping).

Usage (run all 5 simultaneously):
  python reimport_insurance_parallel.py --worker 0 --total 5
  ...
"""
import os, sys, time, logging, argparse, requests
import pandas as pd
import psycopg2, psycopg2.extras
from io import StringIO
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

parser = argparse.ArgumentParser()
parser.add_argument("--worker",       type=int, required=True)
parser.add_argument("--total",        type=int, default=5)
parser.add_argument("--no-truncate",  action="store_true", help="Skip truncate (use when other workers are already running)")
args = parser.parse_args()

WID          = args.worker
TOTAL        = args.total
DB_URL       = os.getenv("SUPABASE_DB_URL")
APP_TOKEN    = os.getenv("SOCRATA_APP_TOKEN", "")
INSHIST_ID   = "6sqe-dvqs"
ACTPEND_ID   = "ypjt-5ydn"
PAGE_SIZE    = 50_000
HEADERS      = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [InsW{WID}] %(message)s",
    handlers=[
        logging.FileHandler(f"reimport_insurance_w{WID}.log", encoding="utf-8"),
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

def get_total_rows(dataset_id):
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{dataset_id}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30,
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return 7_600_000 if dataset_id == INSHIST_ID else 500_000

def fetch_page(dataset_id, offset):
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{dataset_id}.csv",
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
                log.warning(f"  Failed {dataset_id} offset {offset}: {e}")
                return pd.DataFrame()
            time.sleep(2 ** attempt)
    return pd.DataFrame()

def to_rows_inshist(df):
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0": continue
        status = sv(r.get("mod_col_1") or r.get("status"))
        cancel = safe_date(r.get("cancl_effective_date") or r.get("cancellation_date") or r.get("cancel_date"))
        rows.append({
            "dot_number":        dot,
            "policy_type":       sv(r.get("ins_form_code") or r.get("mod_col_3") or r.get("type_of_insurance")),
            "insurer_name":      sv(r.get("name_company") or r.get("insurance_company")),
            "policy_number":     sv(r.get("policy_no") or r.get("policy_number")),
            "effective_date":    safe_date(r.get("effective_date")),
            "cancellation_date": cancel,
            "status":            status,
        })
    return rows

def build_docket_to_dot(conn):
    mapping = {}
    with conn.cursor() as cur:
        cur.execute("SELECT mc_number, dot_number FROM carriers WHERE mc_number IS NOT NULL AND dot_number IS NOT NULL")
        for mc, dot in cur.fetchall():
            if mc and dot:
                mapping[mc] = dot
    log.info(f"  Docket→DOT mapping: {len(mapping):,} entries")
    return mapping

def to_rows_actpend(df, docket_to_dot):
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            docket = sv(r.get("prefix_docket_number"))
            dot = docket_to_dot.get(docket) if docket else None
        if not dot or dot == "0": continue
        cancel = safe_date(r.get("cancel_effective_date"))
        rows.append({
            "dot_number":        dot,
            "policy_type":       sv(r.get("ins_form_code") or r.get("mod_col_3")),
            "insurer_name":      sv(r.get("name_company") or r.get("insurance_company")),
            "policy_number":     sv(r.get("policy_no") or r.get("policy_number")),
            "effective_date":    safe_date(r.get("effective_date")),
            "cancellation_date": cancel,
            "status":            "Active",
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
                    f"INSERT INTO insurance ({', '.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    [[r[c] for c in cols] for r in rows], page_size=5000,
                )
            conn.commit()
            conn.close()
            return
        except Exception as e:
            log.warning(f"  flush attempt {attempt+1} failed: {e}")
            try: conn.close()
            except Exception: pass
            if attempt < 3: time.sleep(3 * (attempt + 1))
    log.error("  flush failed after 4 attempts — skipping page")

def import_dataset(dataset_id, to_rows_fn, label, docket_to_dot=None):
    total_rows = get_total_rows(dataset_id)
    total_pages = (total_rows + PAGE_SIZE - 1) // PAGE_SIZE
    my_pages = list(range(WID, total_pages, TOTAL))
    log.info(f"[{label}] {total_rows:,} rows → {total_pages} pages | Worker {WID}: {len(my_pages)} pages")
    inserted = 0
    for i, page_num in enumerate(my_pages):
        df = fetch_page(dataset_id, page_num * PAGE_SIZE)
        if df.empty:
            log.info(f"  [{label}] Page {page_num}: empty — stopping")
            break
        rows = to_rows_fn(df) if docket_to_dot is None else to_rows_fn(df, docket_to_dot)
        flush(rows)
        inserted += len(rows)
        log.info(f"  [{label}] Page {page_num} ({i+1}/{len(my_pages)}): {len(rows):,} | total: {inserted:,}")
    return inserted

def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set"); sys.exit(1)

    if WID == 0 and not args.no_truncate:
        log.info("Worker 0: truncating insurance table...")
        conn0 = psycopg2.connect(DB_URL, connect_timeout=30)
        with conn0.cursor() as cur:
            cur.execute("TRUNCATE TABLE insurance")
        conn0.commit()
        conn0.close()
        log.info("  Truncated.")
    elif not args.no_truncate:
        log.info(f"Worker {WID}: waiting 15s for Worker 0 to truncate...")
        time.sleep(15)
    else:
        log.info(f"Worker {WID}: --no-truncate mode, starting immediately")

    # Phase 1 — InsHist
    n1 = import_dataset(INSHIST_ID, to_rows_inshist, "InsHist")

    # Phase 2 — ActPendInsur (needs docket→DOT mapping from a short-lived connection)
    log.info(f"[ActPendInsur] Building docket→DOT mapping...")
    conn_map = psycopg2.connect(DB_URL, connect_timeout=30)
    docket_to_dot = build_docket_to_dot(conn_map)
    conn_map.close()
    n2 = import_dataset(ACTPEND_ID, to_rows_actpend, "ActPendInsur", docket_to_dot)

    log.info(f"Worker {WID} DONE: {n1:,} InsHist + {n2:,} ActPendInsur rows inserted.")

if __name__ == "__main__":
    main()
