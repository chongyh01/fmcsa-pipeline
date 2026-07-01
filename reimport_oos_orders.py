"""
OOS orders re-import — truncate + clean reload from FMCSA Out of Service Orders (p2mt-9ige)
=============================================================================================
Fixes duplicate rows from repeated imports (was 780,668 rows / 390,296 duplicate
groups on key dot_number+order_date+reason, source dataset has only ~390K rows).

- Truncates `oos_orders` once at the start of a fresh run.
- Pages are marked "done" (and progress saved) ONLY after a successful flush —
  a crash mid-flush can at worst re-do one page (~50K rows), not millions.
- Progress saved to fmcsa_cache/oos_orders_v2_progress.json.
- Mirrors the column mapping of load_oos_orders() in fmcsa_import.py:
    dot_number          <- dot_number
    order_date          <- oos_date or order_date (fallback: today)
    effective_date      <- effective_date (not present in source -> NULL)
    reinstatement_date  <- rescind_date or reinstatement_date
    order_type          <- order_type (not present in source -> NULL)
    reason              <- oos_reason or reason
    status              <- status (source ACTIVE/INACTIVE), else REINSTATED/ACTIVE fallback
"""

import os, sys, json, time, logging, requests
import pandas as pd
import psycopg2, psycopg2.extras
from io import StringIO
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("reimport_oos_orders.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

APP_TOKEN  = os.getenv("SOCRATA_APP_TOKEN", "")
DB_URL     = os.getenv("SUPABASE_DB_URL")
DATASET_ID = "p2mt-9ige"
PAGE_SIZE  = 50_000
DL_THREADS = 20
DL_BATCH   = 10
CACHE_DIR  = Path("fmcsa_cache")
PROGRESS_F = CACHE_DIR / "oos_orders_v2_progress.json"

CACHE_DIR.mkdir(exist_ok=True)
HEADERS = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}


def sv(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip()
    if s.lower() in ("nan", "none", ""):
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def safe_date(v):
    if v is None or v == "":
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


def fetch_page(offset, total):
    # Socrata can return a clamped/repeated last page for offsets >= total
    # instead of an empty result, which would create duplicate rows.
    if offset >= total:
        return offset, pd.DataFrame()
    params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{DATASET_ID}.csv",
                params=params, headers=HEADERS, timeout=60
            )
            if r.status_code == 400:
                return offset, pd.DataFrame()
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            return offset, df
        except Exception as e:
            if attempt == 4:
                log.warning(f"  Skipping offset {offset}: {e}")
                return offset, None
            time.sleep(2 ** attempt)


def get_total_rows():
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{DATASET_ID}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return 400_000


def to_rows(df):
    rows = []
    today = date.today()
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        reinstatement = safe_date(r.get("rescind_date") or r.get("reinstatement_date"))
        source_status = sv(r.get("status"))
        status = source_status or ("REINSTATED" if reinstatement else "ACTIVE")
        order_date = safe_date(r.get("oos_date") or r.get("order_date")) or today
        reason = sv(r.get("oos_reason") or r.get("reason"))
        rows.append((
            dot,
            order_date,
            safe_date(r.get("effective_date")),
            reinstatement,
            sv(r.get("order_type")),
            reason,
            status,
        ))
    return rows


def flush_to_db(rows):
    if not rows:
        return True
    for attempt in range(4):
        conn = None
        try:
            conn = psycopg2.connect(DB_URL, connect_timeout=10)
            with conn:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SET synchronous_commit = off")
                    except Exception:
                        pass
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO oos_orders
                           (dot_number, order_date, effective_date,
                            reinstatement_date, order_type, reason, status)
                           VALUES %s""",
                        rows, page_size=10_000,
                    )
            return True
        except Exception as e:
            log.warning(f"  flush attempt {attempt+1} failed: {e}")
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
    log.error("  flush_to_db failed after 4 attempts")
    return False


# ── Setup ──────────────────────────────────────────────────────────────────
if PROGRESS_F.exists():
    completed = set(json.loads(PROGRESS_F.read_text()))
    log.info(f"Resuming: {len(completed)} pages already done")
else:
    completed = set()
    log.info("Fresh start — truncating oos_orders table")
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE oos_orders")
    conn.close()
    log.info("  Cleared.")

total = get_total_rows()
all_offsets = list(range(0, total + PAGE_SIZE, PAGE_SIZE))
pending = [o for o in all_offsets if o not in completed]
log.info(f"  {total:,} rows | {len(all_offsets)} pages total | {len(pending)} pages remaining")

t0 = time.time()
total_inserted = 0

for batch_start in range(0, len(pending), DL_BATCH):
    batch = pending[batch_start: batch_start + DL_BATCH]
    results = {}
    with ThreadPoolExecutor(max_workers=min(DL_THREADS, len(batch))) as pool:
        futs = {pool.submit(fetch_page, off, total): off for off in batch}
        for fut in as_completed(futs):
            off, df = fut.result()
            results[off] = df

    for off in batch:
        df = results.get(off)
        if df is None:
            log.warning(f"  download failed for offset {off} — will retry next run")
            continue
        rows = to_rows(df) if not df.empty else []
        if flush_to_db(rows):
            completed.add(off)
            total_inserted += len(rows)
            PROGRESS_F.write_text(json.dumps(list(completed)))
        else:
            log.error(f"  giving up on offset {off} for now — will retry next run")

    elapsed = time.time() - t0
    rate = total_inserted / elapsed if elapsed else 0
    pct = len(completed) / len(all_offsets) * 100
    eta = (len(all_offsets) - len(completed)) * PAGE_SIZE / rate / 60 if rate > 0 else 0
    log.info(f"  {len(completed)}/{len(all_offsets)} pages ({pct:.0f}%) | "
             f"{total_inserted:,} rows | {rate:.0f} rows/s | ETA ~{eta:.0f}min")

if len(completed) >= len(all_offsets):
    PROGRESS_F.unlink(missing_ok=True)
    log.info("=" * 60)
    log.info(f"DONE. {total_inserted:,} rows inserted in {(time.time()-t0)/60:.1f} min")
    log.info("=" * 60)
else:
    log.info(f"Incomplete this run: {len(completed)}/{len(all_offsets)} pages done — re-run to continue")
