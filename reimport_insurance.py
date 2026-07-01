"""
Insurance re-import — truncate + clean reload from FMCSA Insurance History (6sqe-dvqs)
and Insurance Active/Pending (ypjt-5ydn)
=========================================================================================
Fixes duplicate rows from the original incomplete/resumed import (was 10.59M, should be ~7.97M).

- Truncates `insurance` once at the start of a fresh run.
- Pages are marked "done" (and progress saved) ONLY after a successful flush —
  a crash mid-flush can at worst re-do one page (~50K rows), not millions.
- Progress saved to fmcsa_cache/insurance_v2_progress.json (separate from the
  stale cache left by the original importer). Imports insurance_history first,
  then insurance_active.
"""

import os, sys, json, time, logging, requests
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
        logging.FileHandler("reimport_insurance.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

APP_TOKEN  = os.getenv("SOCRATA_APP_TOKEN", "")
# Port 5432 = direct Postgres (supports COPY FROM STDIN). Port 6543 = PgBouncer (does not).
DB_URL     = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
PAGE_SIZE  = 50_000
DL_THREADS = 20
DL_BATCH   = 10
CACHE_DIR  = Path("fmcsa_cache")
PROGRESS_F = CACHE_DIR / "insurance_v2_progress.json"

CACHE_DIR.mkdir(exist_ok=True)
HEADERS = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

DATASETS = [
    # insurance_history: uses dot_number directly
    ("insurance_history", "6sqe-dvqs", 7_600_000, "dot_number"),
    # insurance_active (ActPendInsur): uses prefix_docket_number, no dot_number field
    ("insurance_active",  "ypjt-5ydn",   500_000, "prefix_docket_number"),
]


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


def fetch_page(dataset_id, offset):
    params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
    for attempt in range(5):
        try:
            r = requests.get(
                f"https://data.transportation.gov/resource/{dataset_id}.csv",
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


def get_total_rows(dataset_id, default):
    try:
        r = requests.get(
            f"https://data.transportation.gov/resource/{dataset_id}.json",
            params={"$select": "count(:id)"}, headers=HEADERS, timeout=30
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return default


def to_rows(df, docket_to_dot=None):
    """
    Convert a DataFrame page to insurance row tuples.
    docket_to_dot: if provided, maps prefix_docket_number → dot_number (for ActPendInsur).
    """
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("dot_number"))
        if (not dot or dot == "0") and docket_to_dot is not None:
            # ActPendInsur path: resolve via docket number
            docket = sv(r.get("prefix_docket_number"))
            dot = docket_to_dot.get(docket) if docket else None
        if not dot or dot == "0":
            continue

        # ActPendInsur has no status field — mark as Active; cancel_date null = open policy
        if docket_to_dot is not None:
            status = "Active"
            cancel = safe_date(r.get("cancel_effective_date"))
        else:
            status = sv(r.get("mod_col_1") or r.get("status"))
            cancel = safe_date(r.get("cancl_effective_date") or r.get("cancellation_date") or r.get("cancel_date"))

        rows.append((
            dot,
            sv(r.get("ins_form_code") or r.get("mod_col_3") or r.get("type_of_insurance") or r.get("ins_type")),
            sv(r.get("name_company") or r.get("insurance_company") or r.get("insurer_name")),
            sv(r.get("policy_no") or r.get("policy_number")),
            safe_date(r.get("effective_date")),
            cancel,
            status,
        ))
    return rows


def _copy_escape(v):
    """Escape a value for Postgres COPY text format."""
    if v is None:
        return r"\N"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

def flush_to_db(rows):
    """COPY FROM STDIN — 3-5x faster than execute_values. Requires port 5432 direct connection."""
    if not rows:
        return True
    from io import StringIO
    buf = StringIO()
    for row in rows:
        # rows are tuples: (dot_number, policy_type, insurer_name, policy_number, effective_date, cancellation_date, status)
        buf.write("\t".join(_copy_escape(v) for v in row) + "\n")
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
                        """COPY insurance (dot_number, policy_type, insurer_name, policy_number,
                           effective_date, cancellation_date, status) FROM STDIN""",
                        buf,
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
    state = json.loads(PROGRESS_F.read_text())
    log.info("Resuming from saved progress")
else:
    state = {"truncated": False}
    log.info("Fresh start — truncating insurance table")
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    with conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE insurance")
    conn.close()
    state["truncated"] = True
    PROGRESS_F.write_text(json.dumps(state))
    log.info("  Cleared.")

def build_docket_to_dot():
    """
    Build a full docket_number → dot_number mapping from the carriers table.
    Used by ActPendInsur import to resolve prefix_docket_number → dot_number.
    Requires carriers.mc_number to be correctly populated (run fix_mc_and_fleet.py first).
    """
    mapping: dict[str, str] = {}
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mc_number, dot_number FROM carriers WHERE mc_number IS NOT NULL AND dot_number IS NOT NULL")
            for mc, dot in cur.fetchall():
                if mc and dot:
                    mapping[mc] = dot
        log.info(f"  Docket→DOT mapping: {len(mapping):,} entries loaded from carriers table")
    finally:
        conn.close()
    return mapping


t0 = time.time()
total_inserted = 0

for name, dataset_id, default_total, key_field in DATASETS:
    completed = set(state.get(name, []))
    total = get_total_rows(dataset_id, default_total)
    all_offsets = list(range(0, total + PAGE_SIZE, PAGE_SIZE))
    pending = [o for o in all_offsets if o not in completed]

    if not pending:
        log.info(f"{name}: already complete ({len(all_offsets)} pages) — skipping")
        continue

    log.info("=" * 60)
    log.info(f"{name} ({dataset_id}): {total:,} rows | {len(all_offsets)} pages total | "
             f"{len(pending)} pages remaining | key: {key_field}")
    log.info("=" * 60)

    # ActPendInsur needs a pre-built docket→DOT mapping
    docket_to_dot = build_docket_to_dot() if key_field == "prefix_docket_number" else None
    if key_field == "prefix_docket_number" and not docket_to_dot:
        log.error("  docket→DOT mapping is empty — carriers table may not have mc_number populated.")
        log.error("  Run fix_mc_and_fleet.py first, then re-run this script.")
        continue

    for batch_start in range(0, len(pending), DL_BATCH):
        batch = pending[batch_start: batch_start + DL_BATCH]
        results = {}
        with ThreadPoolExecutor(max_workers=min(DL_THREADS, len(batch))) as pool:
            futs = {pool.submit(fetch_page, dataset_id, off): off for off in batch}
            for fut in as_completed(futs):
                off, df = fut.result()
                results[off] = df

        for off in batch:
            df = results.get(off)
            if df is None:
                log.warning(f"  download failed for offset {off} — will retry next run")
                continue
            rows = to_rows(df, docket_to_dot) if not df.empty else []
            if flush_to_db(rows):
                completed.add(off)
                total_inserted += len(rows)
                state[name] = list(completed)
                PROGRESS_F.write_text(json.dumps(state))
            else:
                log.error(f"  giving up on offset {off} for now — will retry next run")

        elapsed = time.time() - t0
        rate = total_inserted / elapsed if elapsed else 0
        pct = len(completed) / len(all_offsets) * 100
        log.info(f"  [{name}] {len(completed)}/{len(all_offsets)} pages ({pct:.0f}%) | "
                 f"{total_inserted:,} total rows | {rate:.0f} rows/s")

    if len(completed) >= len(all_offsets):
        log.info(f"{name}: complete.")
    else:
        log.info(f"{name}: incomplete this run ({len(completed)}/{len(all_offsets)}) — re-run to continue")

# Done only if both datasets fully complete
all_done = True
for name, dataset_id, default_total, _key in DATASETS:
    total = get_total_rows(dataset_id, default_total)
    all_offsets = list(range(0, total + PAGE_SIZE, PAGE_SIZE))
    if len(set(state.get(name, []))) < len(all_offsets):
        all_done = False

if all_done:
    PROGRESS_F.unlink(missing_ok=True)
    log.info("=" * 60)
    log.info(f"DONE. {total_inserted:,} rows inserted in {(time.time()-t0)/60:.1f} min")
    log.info("=" * 60)
else:
    log.info("Run incomplete — re-run to continue")
