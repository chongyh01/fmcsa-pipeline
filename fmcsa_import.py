"""
FMCSA Data Import Script v5
============================
Carrier Intelligence Platform

IMPROVEMENTS over v4:
  - Socrata app token support (SOCRATA_APP_TOKEN env var) — higher rate limits
  - True pipelined fetch+write: all pages submitted to thread pool at once,
    each written to DB the moment its fetch lands (no more batch-fetch-then-write)
  - Loaders use to_dict(orient='records') instead of iterrows() — 2-5x faster
  - load_violations uses dot_number directly when available — no per-page API roundtrip

MODES:
  python fmcsa_import.py --mode initial              # Full load
  python fmcsa_import.py --mode initial --skip-carriers  # Skip carriers
  python fmcsa_import.py --mode initial --only crash_file violations sms_output_ab
  python fmcsa_import.py --mode daily               # Daily sync

SETUP:
  pip install requests pandas psycopg2-binary python-dotenv tqdm
  Optional: set SOCRATA_APP_TOKEN in .env for higher API rate limits
"""

import os, sys, argparse, logging, requests, time, json
import pandas as pd
import psycopg2
import psycopg2.extras
from pathlib import Path
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("fmcsa_import.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_URL = os.getenv("SUPABASE_DB_URL")

# ============================================================
# SETTINGS — tuned for speed
# ============================================================
PAGE_SIZE    = 50000   # rows per API request (Socrata max)
PAGE_THREADS = 10       # parallel page downloads
BATCH_SIZE   = 2000    # rows per DB insert
CACHE_DIR    = Path("fmcsa_cache")
CACHE_DIR.mkdir(exist_ok=True)
APP_TOKEN    = os.getenv("SOCRATA_APP_TOKEN", "")  # register free at data.transportation.gov


def get_conn():
    return psycopg2.connect(DB_URL, connect_timeout=30)


# ============================================================
# FILE DEFINITIONS
# ============================================================
INITIAL_FILES = {
    "company_census": {
        "dataset_id": "az4n-8mr2",
        "table": "carriers",
        "description": "Company Census (~4.4M carriers)",
        "loader": "load_carriers",
    },
    "crash_file": {
        "dataset_id": "aayw-vxb3",
        "table": "crashes",
        "description": "Crash File (~1.5M crashes)",
        "loader": "load_crashes",
    },
    "vehicle_inspections": {
        "dataset_id": "fx4q-ay7w",
        "table": "inspections",
        "description": "Vehicle Inspections (3yr)",
        "loader": "load_inspections",
    },
    "violations": {
        "dataset_id": "876r-jsdb",
        "table": "violations",
        "description": "Violations (3yr)",
        "loader": "load_violations",
    },
    "authority_history": {
        "dataset_id": "9mw4-x3tu",
        "table": "authority_history",
        "description": "Authority History",
        "loader": "load_authority_history",
    },
    "insurance_history": {
        "dataset_id": "6sqe-dvqs",
        "table": "insurance",
        "description": "Insurance History",
        "loader": "load_insurance",
    },
    "insurance_active": {
        "dataset_id": "ypjt-5ydn",
        "table": "insurance",
        "description": "Active/Pending Insurance",
        "loader": "load_insurance_active",
    },
    "revocation_history": {
        "dataset_id": "sa6p-acbp",
        "table": "carrier_alerts",
        "description": "Revocation History",
        "loader": "load_revocations",
    },
    "oos_orders": {
        "dataset_id": "p2mt-9ige",
        "table": "oos_orders",
        "description": "Out of Service Orders",
        "loader": "load_oos_orders",
    },
    "citations": {
        "dataset_id": "qbt8-7vic",
        "table": "citations",
        "description": "Inspections & Citations",
        "loader": "load_citations",
    },
    "sms_output_ab": {
        "dataset_id": "m3ry-qcip",
        "table": "sms_scores",
        "description": "SMS Output - carriers",
        "loader": "load_sms_scores",
    },
    "boc3_all": {
        "dataset_id": "6snj-ed7q",
        "table": "boc3",
        "description": "BOC3 Process Agents – All With History",
        "loader": "load_boc3",
    },
    "rejected_insurance_all": {
        "dataset_id": "96tg-4mhf",
        "table": "rejected_insurance",
        "description": "Rejected Insurance – All With History",
        "loader": "load_rejected_insurance",
    },
}

DAILY_FILES = {
    "revocation_daily": {
        "dataset_id": "pivg-szje",
        "table": "carrier_alerts",
        "description": "Revocations (daily diff)",
        "loader": "load_revocations",
    },
    "insurance_daily": {
        "dataset_id": "xkmg-ff2t",
        "table": "insurance",
        "description": "Insurance changes (daily diff)",
        "loader": "load_insurance",
    },
    "oos_orders_daily": {
        "dataset_id": "p2mt-9ige",
        "table": "oos_orders",
        "description": "OOS Orders (daily check)",
        "loader": "load_oos_orders",
    },
    "authority_daily": {
        "dataset_id": "sn3k-dnx7",
        "table": "authority_history",
        "description": "Authority changes (daily diff)",
        "loader": "load_authority_history",
    },
}


# ============================================================
# RESUME CACHE
# ============================================================
def cache_path(name):
    return CACHE_DIR / f"{name}_progress.json"

def load_progress(name):
    p = cache_path(name)
    if p.exists():
        data = json.loads(p.read_text())
        return set(data.get("completed_offsets", []))
    return set()

def save_progress(name, completed_offsets):
    cache_path(name).write_text(
        json.dumps({"completed_offsets": list(completed_offsets), "updated": str(datetime.now())})
    )

def clear_progress(name):
    p = cache_path(name)
    if p.exists():
        p.unlink()


# ============================================================
# HELPERS
# ============================================================
def safe_date(val):
    if val is None or val == "":
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None

def sv(val):
    """Safe string — strips float .0 suffix from DOT numbers."""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except Exception:
        pass
    s = str(val).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None

def iv(val, default=0):
    try:
        return int(float(val or default))
    except (ValueError, TypeError):
        return default

def norm(df):
    df.columns = [c.lower() for c in df.columns]
    return df


# ============================================================
# DB WRITE — single connection, large batches
# ============================================================
def insert_rows(conn, table, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join([f"%({c})s" for c in cols])
    col_list = ", ".join(cols)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()

def upsert_rows(conn, table, rows, conflict_cols):
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join([f"%({c})s" for c in cols])
    col_list = ", ".join(cols)
    update_set = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict_cols])
    conflict = ", ".join(conflict_cols)
    sql = f"""
        INSERT INTO {table} ({col_list}) VALUES ({placeholders})
        ON CONFLICT ({conflict}) DO UPDATE SET {update_set}
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=BATCH_SIZE)
    conn.commit()


# ============================================================
# FETCH
# ============================================================
def get_total_rows(dataset_id):
    try:
        url = f"https://data.transportation.gov/resource/{dataset_id}.json?$select=count(*)&$limit=1"
        headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return int(r.json()[0]["count"])
    except Exception as e:
        log.warning(f"  Could not get row count: {e} — estimating 500k")
        return 500000

def fetch_page(dataset_id, offset, retries=5):
    url = f"https://data.transportation.gov/resource/{dataset_id}.csv"
    params = {"$limit": PAGE_SIZE, "$offset": offset}
    headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=180)
            # 400 at high offsets means we're past the end of the dataset — not a real error
            if r.status_code == 400:
                log.info(f"  Page offset={offset} returned 400 (past end of dataset) — treating as empty")
                return offset, pd.DataFrame()
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), low_memory=False)
            return offset, df
        except Exception as e:
            if attempt < retries:
                wait = attempt * 15
                log.warning(f"  Page offset={offset} attempt {attempt} failed: {e} — retrying in {wait}s")
                time.sleep(wait)
            else:
                log.error(f"  Page offset={offset} failed after {retries} attempts: {e}")
                return offset, None


# ============================================================
# DOWNLOAD + LOAD  (pipelined: fetch overlaps write)
# ============================================================
def download_and_load(name, dataset_id, loader_fn):
    log.info(f"Starting: {name}")

    total_rows = get_total_rows(dataset_id)
    all_offsets = list(range(0, total_rows + PAGE_SIZE, PAGE_SIZE))
    completed = load_progress(name)
    pending = [o for o in all_offsets if o not in completed]

    if not pending:
        log.info(f"  ALREADY COMPLETE: {name} — skipping")
        return True

    if completed:
        log.info(f"  RESUMING: {name} — {len(completed)}/{len(all_offsets)} pages done, {len(pending)} remaining")
    else:
        log.info(f"  Total rows: {total_rows:,} — {len(all_offsets)} pages to fetch")

    rows_written = 0
    failed_offsets = []

    with tqdm(total=len(pending), unit=" pages", desc=name) as bar:
        # Submit ALL pending pages at once — executor caps concurrency at PAGE_THREADS.
        # As each fetch completes the main thread writes immediately, so fetches and
        # writes overlap rather than alternating in batches.
        with ThreadPoolExecutor(max_workers=PAGE_THREADS) as executor:
            futures = {executor.submit(fetch_page, dataset_id, offset): offset for offset in pending}

            for future in as_completed(futures):
                offset, df = future.result()

                if df is None:
                    failed_offsets.append(offset)
                    bar.update(1)
                    continue
                if df.empty:
                    # Past end of dataset — count as done, no rows to write
                    completed.add(offset)
                    save_progress(name, completed)
                    bar.update(1)
                    continue

                written = False
                for db_attempt in range(1, 4):
                    try:
                        conn = get_conn()
                        loader_fn(df, conn)
                        conn.close()
                        completed.add(offset)
                        rows_written += len(df)
                        written = True
                        break
                    except Exception as e:
                        log.error(f"  DB write failed at offset={offset} (attempt {db_attempt}): {e}")
                        try:
                            conn.close()
                        except Exception:
                            pass
                        if db_attempt < 3:
                            wait = db_attempt * 15
                            log.info(f"  Retrying offset={offset} in {wait}s...")
                            time.sleep(wait)

                if not written:
                    log.error(f"  GIVING UP on offset={offset} after 3 attempts")
                    failed_offsets.append(offset)

                save_progress(name, completed)
                bar.update(1)

    if failed_offsets:
        log.warning(f"  {len(failed_offsets)} pages failed for {name}")

    log.info(f"  DONE: {name} — {rows_written:,} rows written ({len(completed)}/{len(all_offsets)} pages)")

    if len(failed_offsets) == 0:
        clear_progress(name)

    return len(failed_offsets) == 0


# ============================================================
# LOADERS
# ============================================================
STATUS_CODE_MAP = {"A": "ACTIVE", "I": "INACTIVE", "X": "OUT-OF-SERVICE", "N": "NOT AUTHORIZED"}

def load_carriers(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot:
            continue
        # Build MC number zero-padded to 6-digit suffix so it matches ActPendInsur
        # prefix_docket_number format ("MC000074", "MC771154", etc.)
        mc = None
        for i in ("1", "2", "3"):
            prefix = sv(r.get(f"docket{i}prefix"))
            number = sv(r.get(f"docket{i}"))
            if prefix and number:
                mc = f"{prefix}{number.zfill(6)}"
                break
        status_raw = sv(r.get("status_code"))
        rows.append({
            "dot_number":         dot,
            "mc_number":          mc,
            "legal_name":         sv(r.get("legal_name")),
            "dba_name":           sv(r.get("dba_name")),
            "address":            sv(r.get("phy_street")),
            "city":               sv(r.get("phy_city")),
            "state":              sv(r.get("phy_state")),
            "zip":                sv(r.get("phy_zip")),
            "phone":              sv(r.get("phone") or r.get("telephone")),
            "total_drivers":      iv(r.get("total_drivers")),
            # truck_units = CMV trucks only (Socrata field); power_units includes non-CMV
            # power_units in Socrata = truck_units + total_cars; we store CMV count only
            "total_trucks":       iv(r.get("truck_units") or 0),
            "cargo_type":         sv(r.get("classdef") or r.get("cargo_carried_id")),
            # carrier_operation: 'A'=Interstate, 'B'=Intrastate HM, 'C'=Intrastate Non-HM
            "carrier_operation":  sv(r.get("carrier_operation")),
            # non_cmv_units = total_cars (light vehicles registered but not CMV trucks)
            "non_cmv_units":      iv(r.get("total_cars") or 0),
            # has_passenger_cargo: 'X' in crgo_passengers field
            "has_passenger_cargo": bool(sv(r.get("crgo_passengers"))),
            "status":             STATUS_CODE_MAP.get(status_raw, status_raw) if status_raw else None,
            "safety_rating":      sv(r.get("safety_rating")),
            "safety_rating_date": safe_date(r.get("safety_rating_date")),
            "review_type":        sv(r.get("review_type")),
            "review_date":        safe_date(r.get("review_date")),
        })
    upsert_rows(conn, "carriers", rows, ["dot_number"])


def load_crashes(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number") or r.get("upload_dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":    dot,
            "crash_date":    safe_date(r.get("crash_date") or r.get("report_date")),
            "state":         (sv(r.get("report_state")) or "")[:2] or None,
            "fatal":         iv(r.get("fatalities")),
            "injury":        iv(r.get("injuries")),
            "towaway":       iv(r.get("tow_away")),
            "report_number": sv(r.get("report_number")),
        })
    insert_rows(conn, "crashes", rows)


def load_inspections(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":       dot,
            "inspection_date":  safe_date(r.get("insp_date") or r.get("inspection_date")),
            "state":            (sv(r.get("report_state") or r.get("insp_state")) or "")[:2] or None,
            "level":            sv(r.get("insp_level_id") or r.get("level_id")),
            "oos_vehicles":     iv(r.get("oos_total")),
            "oos_drivers":      iv(r.get("driver_oos_total") or r.get("drv_oos_total")),
            "total_violations": iv(r.get("viol_total") or r.get("total_violations")),
        })
    insert_rows(conn, "inspections", rows)


CFR_PART_BASIC = {
    "382": "Controlled Substances/Alcohol", "383": "Driver Fitness",
    "384": "Driver Fitness",               "391": "Driver Fitness",
    "392": "Unsafe Driving",               "393": "Vehicle Maintenance",
    "395": "Hours-of-Service Compliance",  "396": "Vehicle Maintenance",
    "397": "Hazardous Materials",          "177": "Hazardous Materials",
    "178": "Hazardous Materials",          "180": "Hazardous Materials",
    "385": "Vehicle Maintenance",          "390": "Vehicle Maintenance",
}

def map_basic(part_no):
    if not part_no:
        return None
    return CFR_PART_BASIC.get(str(part_no).split(".")[0], "Vehicle Maintenance")


def _build_insp_cache(inspection_ids):
    """Fallback: fetch dot_numbers for inspection_ids from FMCSA API."""
    if not inspection_ids:
        return {}
    ids_str = ",".join(f"'{i}'" for i in inspection_ids[:500])
    url = f"https://data.transportation.gov/resource/fx4q-ay7w.csv?$where=inspection_id in ({ids_str})&$select=inspection_id,dot_number&$limit=500"
    headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), low_memory=False)
        df = norm(df)
        return {str(row["inspection_id"]): sv(row["dot_number"])
                for _, row in df.iterrows()
                if sv(row.get("dot_number")) and sv(row.get("dot_number")) != "0"}
    except Exception as e:
        log.warning(f"  Could not fetch inspection cache: {e}")
        return {}


def load_violations(df, conn):
    df = norm(df)

    # FMCSA violations dataset (876r-jsdb) includes dot_number directly.
    # Only fall back to the inspection API lookup if the column is absent/empty.
    has_dot = "dot_number" in df.columns and df["dot_number"].notna().any()

    if not has_dot:
        insp_ids = list(set(str(r) for r in df["inspection_id"].dropna() if str(r) != "nan"))
        insp_cache = _build_insp_cache(insp_ids)
    else:
        insp_cache = {}

    rows = []
    for r in df.to_dict(orient='records'):
        if has_dot:
            dot = sv(r.get("dot_number"))
        else:
            insp_id = sv(r.get("inspection_id"))
            dot = insp_cache.get(str(insp_id) if insp_id else "")

        if not dot or dot == "0":
            continue

        part = sv(r.get("part_no"))
        sect = sv(r.get("part_no_section") or r.get("description"))
        cfr = f"{part}.{sect}" if part and sect else (sect or part)
        code = sv(r.get("insp_violation_category_id") or r.get("violation_code"))
        oos = sv(r.get("out_of_service_indicator") or r.get("oos"))
        unit = sv(r.get("insp_viol_unit") or r.get("unit_type"))

        rows.append({
            "dot_number":     dot,
            "violation_code": code,
            "description":    cfr,
            "oos_indicator":  (oos or "N")[:1],
            "unit_type":      unit,
            "basic_category": map_basic(part or code),
        })
    insert_rows(conn, "violations", rows)


def load_authority_history(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
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
    insert_rows(conn, "authority_history", rows)


def load_insurance(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":        dot,
            "policy_type":       sv(r.get("ins_form_code") or r.get("mod_col_3") or r.get("type_of_insurance") or r.get("ins_type")),
            "insurer_name":      sv(r.get("name_company") or r.get("insurance_company") or r.get("insurer_name")),
            "policy_number":     sv(r.get("policy_no") or r.get("policy_number")),
            "effective_date":    safe_date(r.get("effective_date")),
            "cancellation_date": safe_date(r.get("cancl_effective_date") or r.get("cancellation_date") or r.get("cancel_date")),
            "status":            sv(r.get("mod_col_1") or r.get("status")),
        })
    insert_rows(conn, "insurance", rows)


def load_insurance_active(df, conn):
    """
    Load ActPendInsur (dataset ypjt-5ydn — active/pending insurance).
    This dataset identifies carriers by prefix_docket_number (e.g. "MC771154"),
    not by dot_number. We resolve dot_number via a lookup against the carriers table.
    Requires carriers to have correct, zero-padded mc_number values — run
    fix_mc_and_fleet.py first if those values are wrong.
    """
    df = norm(df)
    if "prefix_docket_number" not in df.columns:
        log.warning("  load_insurance_active: prefix_docket_number column absent — no rows loaded")
        return

    # Build docket → DOT mapping from carriers table for all dockets in this page
    unique_dockets = [sv(d) for d in df["prefix_docket_number"].dropna().unique() if sv(d)]
    docket_to_dot: dict[str, str] = {}
    if unique_dockets:
        with conn.cursor() as cur:
            for i in range(0, len(unique_dockets), 500):
                batch = unique_dockets[i : i + 500]
                cur.execute(
                    "SELECT mc_number, dot_number FROM carriers "
                    "WHERE mc_number = ANY(%s) AND dot_number IS NOT NULL",
                    (batch,)
                )
                for mc, dot in cur.fetchall():
                    if mc and dot:
                        docket_to_dot[mc] = dot

    rows = []
    for r in df.to_dict(orient="records"):
        docket = sv(r.get("prefix_docket_number"))
        dot = docket_to_dot.get(docket) if docket else None
        if not dot:
            continue
        rows.append({
            "dot_number":        dot,
            "policy_type":       sv(r.get("ins_form_code")),
            "insurer_name":      sv(r.get("name_company")),
            "policy_number":     sv(r.get("policy_no")),
            "effective_date":    safe_date(r.get("effective_date")),
            "cancellation_date": safe_date(r.get("cancel_effective_date")),
            "status":            "Active",
        })
    insert_rows(conn, "insurance", rows)


def load_revocations(df, conn):
    df = norm(df)
    rows = []
    today = date.today()
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":  dot,
            "event_type":  "INVOLUNTARY_REVOCATION",
            "event_date":  safe_date(r.get("order2_effective_date") or r.get("order1_serve_date") or r.get("revocation_date") or r.get("action_date")) or today,
            "description": sv(r.get("order2_type_desc") or r.get("reason")) or "Authority revoked by FMCSA",
            "source_file": "REVOCATION",
        })
    insert_rows(conn, "carrier_alerts", rows)


def load_oos_orders(df, conn):
    df = norm(df)
    oos_rows = []
    alert_rows = []
    today = date.today()
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        reinstatement = safe_date(r.get("rescind_date") or r.get("reinstatement_date"))
        source_status = sv(r.get("status"))
        status = source_status or ("REINSTATED" if reinstatement else "ACTIVE")
        order_date = safe_date(r.get("oos_date") or r.get("order_date")) or today
        reason = sv(r.get("oos_reason") or r.get("reason"))
        oos_rows.append({
            "dot_number":         dot,
            "order_date":         order_date,
            "effective_date":     safe_date(r.get("effective_date")),
            "reinstatement_date": reinstatement,
            "order_type":         sv(r.get("order_type")),
            "reason":             reason,
            "status":             status,
        })
        if status not in ("REINSTATED", "RESCINDED"):
            alert_rows.append({
                "dot_number":  dot,
                "event_type":  "OOS_ORDER",
                "event_date":  order_date,
                "description": f"Out of Service Order. Reason: {reason or 'Not specified'}",
                "source_file": "OUT_OF_SERVICE_ORDERS",
            })
    insert_rows(conn, "oos_orders", oos_rows)
    if alert_rows:
        insert_rows(conn, "carrier_alerts", alert_rows)


def load_citations(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":    dot,
            "citation_code": sv(r.get("citation_code")),
            "citation_date": safe_date(r.get("citation_date") or r.get("inspection_date")),
            "result":        sv(r.get("result")),
            "description":   sv(r.get("description")),
        })
    insert_rows(conn, "citations", rows)


def load_sms_scores(df, conn):
    df = norm(df)
    score_map = {
        "unsafe_driv_pct":   "unsafe_driving",
        "hos_driv_pct":      "hours_of_service_compliance",
        "driv_fit_pct":      "driver_fitness",
        "contr_subst_pct":   "controlled_substances_alcohol",
        "veh_maint_pct":     "vehicle_maintenance",
    }
    alert_map = {
        "unsafe_driv_basic_alert":   "unsafe_driving_alert",
        "hos_driv_basic_alert":      "hours_of_service_compliance_alert",
        "driv_fit_basic_alert":      "driver_fitness_alert",
        "contr_subst_basic_alert":   "controlled_substances_alcohol_alert",
        "veh_maint_basic_alert":     "vehicle_maintenance_alert",
    }
    today = date.today()
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        row = {"dot_number": dot, "score_date": today}
        for src, dst in score_map.items():
            val = r.get(src)
            try:
                is_nan = pd.isna(val)
            except Exception:
                is_nan = False
            if val is None or is_nan:
                row[dst] = None
            else:
                try:
                    row[dst] = float(str(val).strip().rstrip("%"))
                except (ValueError, TypeError):
                    row[dst] = None
        for src, dst in alert_map.items():
            val = r.get(src)
            try:
                is_nan = pd.isna(val)
            except Exception:
                is_nan = False
            if val is None or is_nan:
                row[dst] = None
            else:
                row[dst] = str(val).strip().upper() in ("Y", "YES", "1", "TRUE")
        rows.append(row)
    upsert_rows(conn, "sms_scores", rows, ["dot_number", "score_date"])


def load_boc3(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("usdot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":     dot,
            "docket_number":  sv(r.get("docket_number")),
            "company_name":   sv(r.get("co_name")),
            "attention_to":   sv(r.get("attn_name")),
            "address":        sv(r.get("street_po")),
            "city":           sv(r.get("city")),
            "state":          sv(r.get("state_code")),
            "country":        sv(r.get("ctry_code")),
            "zip_code":       sv(r.get("zip_code")),
        })
    insert_rows(conn, "boc3", rows)


def load_rejected_insurance(df, conn):
    df = norm(df)
    rows = []
    for r in df.to_dict(orient='records'):
        dot = sv(r.get("dot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":       dot,
            "docket_number":    sv(r.get("docket_number")),
            "form_code":        sv(r.get("ins_form_code")),
            "insurance_type":   sv(r.get("mod_col_1")),
            "policy_number":    sv(r.get("policy_no")),
            "received_date":    safe_date(r.get("recv_date")),
            "class_code":       sv(r.get("ins_class_code") or r.get("mod_col_3")),
            "type_code":        sv(r.get("mod_col_4")),
            "rejected_date":    safe_date(r.get("rej_date")),
            "insurance_branch": sv(r.get("inser_branch")),
            "company_name":     sv(r.get("name_company")),
            "rejected_reason":  sv(r.get("rej_reasons")),
            "min_coverage":     sv(r.get("min_cov_amount")),
        })
    insert_rows(conn, "rejected_insurance", rows)


# ============================================================
# LOADER DISPATCH
# ============================================================
LOADER_FN = {
    "load_carriers":           load_carriers,
    "load_crashes":            load_crashes,
    "load_inspections":        load_inspections,
    "load_violations":         load_violations,
    "load_authority_history":  load_authority_history,
    "load_insurance":          load_insurance,
    "load_insurance_active":   load_insurance_active,
    "load_revocations":        load_revocations,
    "load_oos_orders":         load_oos_orders,
    "load_citations":          load_citations,
    "load_sms_scores":         load_sms_scores,
    "load_boc3":               load_boc3,
    "load_rejected_insurance": load_rejected_insurance,
}


def process_file(name, config):
    start = datetime.now()
    loader_fn = LOADER_FN[config["loader"]]
    success = download_and_load(name, config["dataset_id"], loader_fn)
    elapsed = (datetime.now() - start).seconds
    return (name, success, elapsed)


# ============================================================
# MAIN
# ============================================================
def run(mode, threads=6, skip_carriers=False, only=None, fresh=False):
    files = INITIAL_FILES if mode == "initial" else DAILY_FILES

    if only:
        files = {k: v for k, v in files.items() if k in only}
        log.info(f"Running only: {list(files.keys())}")

    if fresh:
        log.warning("=" * 60)
        log.warning("WARNING: --fresh will DELETE all resume cache and restart from scratch!")
        log.warning("Any progress already downloaded will be lost.")
        log.warning("Press Ctrl+C within 10 seconds to cancel...")
        log.warning("=" * 60)
        time.sleep(10)
        for name in files:
            p = cache_path(name)
            if p.exists():
                p.unlink()
                log.info(f"  Cleared cache for: {name}")

    log.info("=" * 60)
    log.info(f"FMCSA Import v5 - mode: {mode.upper()}")
    log.info(f"Files: {len(files)} | File threads: {threads} | Page threads: {PAGE_THREADS} | Batch: {BATCH_SIZE}")
    log.info(f"App token: {'SET' if APP_TOKEN else 'NOT SET (add SOCRATA_APP_TOKEN to .env for faster imports)'}")
    log.info(f"Resume cache: {CACHE_DIR.absolute()}")
    log.info("=" * 60)

    if mode == "initial" and "company_census" in files and not skip_carriers:
        log.info("Step 1/2 - Loading carriers first (FK dependency)")
        name, success, elapsed = process_file("company_census", files["company_census"])
        if not success:
            log.warning("  carriers had failures — continuing anyway")
        log.info(f"  carriers done in {elapsed}s")
        remaining = {k: v for k, v in files.items() if k != "company_census"}
    else:
        remaining = {k: v for k, v in files.items() if k != "company_census"}

    log.info(f"Step 2/2 - Loading {len(remaining)} files ({threads} parallel)")
    results = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(process_file, name, config): name
            for name, config in remaining.items()
        }
        for future in as_completed(futures):
            results.append(future.result())

    log.info("=" * 60)
    log.info("IMPORT COMPLETE")
    log.info("=" * 60)
    ok = sum(1 for _, s, _ in results if s)
    log.info(f"Succeeded: {ok}/{len(remaining)}")
    for name, success, elapsed in sorted(results, key=lambda x: x[2], reverse=True):
        log.info(f"  [{'OK' if success else 'FAILED'}] {name} ({elapsed}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FMCSA Data Import v5")
    parser.add_argument("--mode", choices=["initial", "daily"], required=True)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--skip-carriers", action="store_true")
    parser.add_argument("--only", nargs="+", help="Run specific files only e.g. --only crash_file violations")
    parser.add_argument("--fresh", action="store_true", help="Clear cache for specified files before running")
    args = parser.parse_args()
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set in .env file")
        sys.exit(1)
    run(args.mode, args.threads, skip_carriers=args.skip_carriers, only=args.only, fresh=args.fresh)
