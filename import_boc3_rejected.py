"""
import_boc3_rejected.py
=======================
Creates and populates two new tables:
  - boc3             (process agents — who to serve legal papers on)
  - rejected_insurance (FMCSA-rejected insurance filings + rejection reasons)

Both tables are high litigation value and were missing from the original import.

Run:
  python import_boc3_rejected.py

Env vars required:
  SUPABASE_DB_URL
Optional:
  SOCRATA_APP_TOKEN
"""
import os, sys, time, logging, requests
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
        logging.FileHandler("import_boc3_rejected.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_URL    = os.getenv("SUPABASE_DB_URL")
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
PAGE_SIZE = 50_000
HEADERS   = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}


def sv(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("nan", "none", "", ".0") else s


def safe_date(v):
    if not v:
        return None
    try:
        import pandas as pd2
        if pd2.isna(v):
            return None
    except Exception:
        pass
    try:
        return str(pd.to_datetime(v).date())
    except Exception:
        return None


def create_tables(conn):
    with conn.cursor() as cur:
        # Create tables if not exist, then truncate — preserves structure and indexes on re-runs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS boc3 (
                id             BIGSERIAL PRIMARY KEY,
                dot_number     TEXT NOT NULL,
                docket_number  TEXT,
                company_name   TEXT,
                attention_to   TEXT,
                address        TEXT,
                city           TEXT,
                state          TEXT,
                country        TEXT,
                zip_code       TEXT,
                imported_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS boc3_dot_number_idx ON boc3(dot_number)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rejected_insurance (
                id               BIGSERIAL PRIMARY KEY,
                dot_number       TEXT NOT NULL,
                docket_number    TEXT,
                form_code        TEXT,
                insurance_type   TEXT,
                policy_number    TEXT,
                received_date    DATE,
                class_code       TEXT,
                type_code        TEXT,
                rejected_date    DATE,
                insurance_branch TEXT,
                company_name     TEXT,
                rejected_reason  TEXT,
                min_coverage     TEXT,
                imported_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS rejected_insurance_dot_number_idx ON rejected_insurance(dot_number)")
        conn.commit()
        # Truncate to clear stale data without dropping structure
        cur.execute("TRUNCATE TABLE boc3 RESTART IDENTITY")
        cur.execute("TRUNCATE TABLE rejected_insurance RESTART IDENTITY")
    conn.commit()
    log.info("Tables ready (truncated).")


def fetch_pages(dataset_id, label):
    all_rows = []
    offset = 0
    page = 1
    while True:
        params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
        for attempt in range(4):
            try:
                r = requests.get(
                    f"https://data.transportation.gov/resource/{dataset_id}.csv",
                    params=params, headers=HEADERS, timeout=120,
                )
                r.raise_for_status()
                df = pd.read_csv(StringIO(r.text), low_memory=False)
                df.columns = [c.strip().lower() for c in df.columns]
                break
            except Exception as e:
                if attempt == 3:
                    log.error(f"  [{label}] Failed page {page}: {e}")
                    df = pd.DataFrame()
                    break
                time.sleep(2 ** attempt)
        if df.empty:
            break
        all_rows.append(df)
        log.info(f"  [{label}] Page {page}: {len(df):,} rows")
        if len(df) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        page += 1
    return pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()


def import_boc3(conn, df):
    rows = []
    for r in df.to_dict(orient="records"):
        dot = sv(r.get("usdot_number"))
        if not dot or dot == "0":
            continue
        rows.append({
            "dot_number":    dot,
            "docket_number": sv(r.get("docket_number")),
            "company_name":  sv(r.get("co_name")),
            "attention_to":  sv(r.get("attn_name")),
            "address":       sv(r.get("street_po")),
            "city":          sv(r.get("city")),
            "state":         sv(r.get("state_code")),
            "country":       sv(r.get("ctry_code")),
            "zip_code":      sv(r.get("zip_code")),
        })
    if not rows:
        log.warning("  No BOC3 rows to insert")
        return 0
    cols = list(rows[0].keys())
    inserted = 0
    for i in range(0, len(rows), 5000):
        batch = rows[i:i + 5000]
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO boc3 ({', '.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    [[r[c] for c in cols] for r in batch], page_size=1000,
                )
            conn.commit()
            inserted += len(batch)
        except Exception as e:
            log.error(f"  BOC3 batch failed: {e}")
            conn.rollback()
    return inserted


def import_rejected(conn, df):
    rows = []
    for r in df.to_dict(orient="records"):
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
    if not rows:
        log.warning("  No rejected_insurance rows to insert")
        return 0
    cols = list(rows[0].keys())
    inserted = 0
    for i in range(0, len(rows), 5000):
        batch = rows[i:i + 5000]
        try:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO rejected_insurance ({', '.join(cols)}) VALUES %s ON CONFLICT DO NOTHING",
                    [[r[c] for c in cols] for r in batch], page_size=1000,
                )
            conn.commit()
            inserted += len(batch)
        except Exception as e:
            log.error(f"  Rejected batch failed: {e}")
            conn.rollback()
    return inserted


def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    conn = psycopg2.connect(DB_URL, connect_timeout=30)

    log.info("Creating tables...")
    create_tables(conn)

    log.info("Fetching BOC3 (6snj-ed7q)...")
    boc3_df = fetch_pages("6snj-ed7q", "BOC3")
    log.info(f"  {len(boc3_df):,} rows downloaded")
    if not boc3_df.empty:
        log.info(f"  Columns: {list(boc3_df.columns)}")
        n = import_boc3(conn, boc3_df)
        log.info(f"  {n:,} BOC3 rows inserted")

    log.info("Fetching Rejected Insurance (96tg-4mhf)...")
    rej_df = fetch_pages("96tg-4mhf", "Rejected")
    log.info(f"  {len(rej_df):,} rows downloaded")
    if not rej_df.empty:
        log.info(f"  Columns: {list(rej_df.columns)}")
        n = import_rejected(conn, rej_df)
        log.info(f"  {n:,} rejected_insurance rows inserted")

    conn.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
