"""
Violations re-import v4 — stable + resumable + fast writes
============================================================
- COPY FROM STDIN instead of execute_values (3-5x faster)
- synchronous_commit=off per flush (async WAL, safe with progress tracking)
- 500K row flush buffer (was 100K)
- Progress saved to fmcsa_cache/violations_progress.json
- Inspection cache saved to fmcsa_cache/violations_insp_cache.json
"""

import os, sys, json, csv, requests, time, logging
import pandas as pd
import psycopg2, psycopg2.extras
from io import StringIO
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
        logging.FileHandler("reimport_violations_fast.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

APP_TOKEN      = os.getenv("SOCRATA_APP_TOKEN", "")
# Use port 5432 (direct Postgres) — COPY FROM STDIN requires direct connection, not PgBouncer (6543)
DB_URL         = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
PAGE_SIZE      = 50_000
DL_THREADS     = 20
BATCH_PAGES    = 10       # increased from 5 — more parallel downloads per batch
FLUSH_SIZE     = 500_000  # increased from 100K — fewer round trips to DB
CACHE_DIR      = Path("fmcsa_cache")
INSP_CACHE_F   = CACHE_DIR / "violations_insp_cache.json"
PROGRESS_F     = CACHE_DIR / "violations_progress.json"

CACHE_DIR.mkdir(exist_ok=True)
HEADERS = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

CFR_PART_BASIC = {
    "382": "Controlled Substances/Alcohol", "383": "Driver Fitness",
    "384": "Driver Fitness",               "391": "Driver Fitness",
    "392": "Unsafe Driving",               "393": "Vehicle Maintenance",
    "395": "Hours-of-Service Compliance",  "396": "Vehicle Maintenance",
    "397": "Hazardous Materials",          "177": "Hazardous Materials",
    "178": "Hazardous Materials",          "180": "Hazardous Materials",
    "385": "Vehicle Maintenance",          "390": "Vehicle Maintenance",
}

def basic_from_part(part):
    return CFR_PART_BASIC.get(str(part).split(".")[0] if part else "", "Vehicle Maintenance")

def sv(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none", "") else None

def fetch_page(dataset_id, offset, select=None):
    params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
    if select:
        params["$select"] = select
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{dataset_id}.csv",
                params=params, headers=HEADERS, timeout=45
            )
            if r.status_code == 400:
                return None
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            return df
        except Exception as e:
            if attempt == 4:
                log.warning(f"  Skipping offset {offset}: {e}")
                return None
            time.sleep(2 ** attempt)

def get_total_rows(dataset_id):
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{dataset_id}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return None

def _copy_escape(v):
    """Escape a value for Postgres COPY text format."""
    if v is None:
        return r"\N"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

def flush_to_db(rows):
    """COPY FROM STDIN — 3-5x faster than execute_values. Requires port 5432 direct connection."""
    if not rows:
        return
    buf = StringIO()
    for r in rows:
        buf.write("\t".join([
            _copy_escape(r.get("dot_number")),
            _copy_escape(r.get("violation_code")),
            _copy_escape(r.get("description")),
            _copy_escape(r.get("oos_indicator")),
            _copy_escape(r.get("unit_type")),
            _copy_escape(r.get("basic_category")),
        ]) + "\n")
    for attempt in range(4):
        conn = None
        try:
            buf.seek(0)
            conn = psycopg2.connect(DB_URL, connect_timeout=10)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 0")
                    cur.execute("SET synchronous_commit = off")
                    cur.copy_expert(
                        """COPY violations (dot_number, violation_code, description,
                           oos_indicator, unit_type, basic_category) FROM STDIN""",
                        buf,
                    )
            return
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
    log.error("  flush_to_db failed after 4 attempts — rows lost for this batch")


# ── STEP 1: Inspection cache ──────────────────────────────────────────────────
if INSP_CACHE_F.exists():
    log.info(f"Loading inspection cache from disk: {INSP_CACHE_F}")
    with open(INSP_CACHE_F) as f:
        insp_cache = json.load(f)
    log.info(f"  {len(insp_cache):,} entries loaded")
else:
    log.info("=" * 60)
    log.info("STEP 1: Building inspection cache (20 threads)")
    log.info("=" * 60)
    total_insp = get_total_rows("fx4q-ay7w") or 14_000_000
    n_pages    = (total_insp + PAGE_SIZE - 1) // PAGE_SIZE
    log.info(f"  ~{total_insp:,} rows | {n_pages} pages")
    insp_cache = {}
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=DL_THREADS) as pool:
        futs = {pool.submit(fetch_page, "fx4q-ay7w", off, "inspection_id,dot_number"): off
                for off in range(0, total_insp + PAGE_SIZE, PAGE_SIZE)}
        for fut in as_completed(futs):
            df = fut.result()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    iid = sv(row.get("inspection_id"))
                    dot = sv(row.get("dot_number"))
                    if iid and dot and dot != "0":
                        insp_cache[iid] = dot
            done += 1
            if done % 50 == 0:
                log.info(f"  {done}/{n_pages} pages | {len(insp_cache):,} entries | {time.time()-t0:.0f}s")
    log.info(f"Cache built: {len(insp_cache):,} entries — saving to disk")
    with open(INSP_CACHE_F, "w") as f:
        json.dump(insp_cache, f)
    log.info("  Saved.")


# ── STEP 2: Check progress / truncate ────────────────────────────────────────
if PROGRESS_F.exists():
    with open(PROGRESS_F) as f:
        completed_offsets = set(json.load(f))
    log.info(f"Resuming: {len(completed_offsets)} pages already done — skipping truncate")
else:
    completed_offsets = set()
    log.info("Fresh start — truncating violations table")
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE violations")
    conn.close()
    log.info("  Cleared.")


# ── STEP 3: Import violations in sequential batches ───────────────────────────
log.info("=" * 60)
log.info("STEP 3: Importing violations (sequential 20-page batches)")
log.info("=" * 60)

total_viol = get_total_rows("876r-jsdb") or 13_500_000
all_offsets = list(range(0, total_viol + PAGE_SIZE, PAGE_SIZE))
pending_offsets = [o for o in all_offsets if o not in completed_offsets]
n_total = len(all_offsets)
log.info(f"  {total_viol:,} rows | {n_total} pages total | {len(pending_offsets)} pages remaining")

total_inserted = 0
pending_rows   = []
t1 = time.time()

for batch_start in range(0, len(pending_offsets), BATCH_PAGES):
    batch = pending_offsets[batch_start : batch_start + BATCH_PAGES]

    # Download this batch of pages in parallel; flush mid-batch to stay under timeout
    with ThreadPoolExecutor(max_workers=min(DL_THREADS, len(batch))) as pool:
        futs = {pool.submit(fetch_page, "876r-jsdb", off): off for off in batch}
        for fut in as_completed(futs):
            df = fut.result()
            if df is not None and not df.empty:
                for r in df.to_dict(orient="records"):
                    iid = sv(r.get("inspection_id"))
                    dot = insp_cache.get(str(iid) if iid else "")
                    if not dot or dot == "0":
                        continue
                    part = sv(r.get("part_no"))
                    sect = sv(r.get("part_no_section"))
                    cfr  = f"{part}.{sect}" if part and sect else (sect or part)
                    pending_rows.append({
                        "dot_number":     dot,
                        "violation_code": sv(r.get("insp_violation_category_id")),
                        "description":    cfr,
                        "oos_indicator":  (sv(r.get("out_of_service_indicator")) or "N")[:1],
                        "unit_type":      sv(r.get("insp_viol_unit")),
                        "basic_category": basic_from_part(part),
                    })
            # Flush as soon as buffer hits limit — keeps each COPY under timeout
            if len(pending_rows) >= FLUSH_SIZE:
                flush_to_db(pending_rows)
                total_inserted += len(pending_rows)
                pending_rows = []

    # Mark pages as done
    for off in batch:
        completed_offsets.add(off)

    # Final flush for any remainder after the batch
    if len(pending_rows) >= FLUSH_SIZE or batch_start + BATCH_PAGES >= len(pending_offsets):
        if pending_rows:
            flush_to_db(pending_rows)
            total_inserted += len(pending_rows)
            pending_rows = []

    # Save progress after every batch
    with open(PROGRESS_F, "w") as f:
        json.dump(list(completed_offsets), f)

    done_pages = len(completed_offsets)
    elapsed = time.time() - t1
    rate = total_inserted / elapsed if elapsed else 0
    pct  = done_pages / n_total * 100
    eta  = (total_viol - total_inserted) / rate / 60 if rate > 0 else 0
    log.info(f"  {done_pages}/{n_total} pages ({pct:.0f}%) | {total_inserted:,} rows | {rate:.0f} rows/s | ETA ~{eta:.0f}min")

# Clean up progress file on success
PROGRESS_F.unlink(missing_ok=True)
elapsed_total = time.time() - t1
log.info("=" * 60)
log.info(f"DONE. {total_inserted:,} violations inserted in {elapsed_total/60:.1f} min")
log.info("=" * 60)
