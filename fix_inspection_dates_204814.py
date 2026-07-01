"""
Fix missing inspection dates for DOT 204814 (BINKS COCA COLA BOTTLING CO).

Both inspection rows in our DB have crash_date = 1970-01-01 (epoch placeholder).
This script:
  1. Queries FMCSA inspection dataset (fx4q-ay7w) on data.transportation.gov
     for DOT 204814 to get the real inspection dates.
  2. Matches each FMCSA row to our DB row by state + level + total_violations.
  3. Updates inspection_date in our DB.

If FMCSA returns more rows than we have (we only store non-compliant ones),
it skips any that don't match a DB row.
"""

import os, sys, requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
DB_URL    = os.getenv("SUPABASE_DB_URL")
DOT       = "204814"

# --- Step 1: fetch our inspection rows for this carrier ---
conn = psycopg2.connect(DB_URL, connect_timeout=15)
cur = conn.cursor()
cur.execute("""
    SELECT id, inspection_date, state, level, total_violations, oos_vehicles, oos_drivers
    FROM inspections
    WHERE dot_number = %s
    ORDER BY id
""", (DOT,))
our_rows = cur.fetchall()
print(f"Our DB has {len(our_rows)} inspection rows for DOT {DOT}:")
for r in our_rows:
    print(f"  id={r[0]}, date={r[1]}, state={r[2]}, level={r[3]}, violations={r[4]}, oos_v={r[5]}, oos_d={r[6]}")

# --- Step 2: fetch from FMCSA Socrata ---
print(f"\nQuerying FMCSA inspection dataset (fx4q-ay7w) for DOT {DOT}...")
headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
params = {
    "$where": f"dot_number='{DOT}'",
    "$limit": 100,
    "$order": "insp_date ASC",
}
r = requests.get(
    "https://data.transportation.gov/resource/fx4q-ay7w.json",
    params=params, headers=headers, timeout=30
)
r.raise_for_status()
fmcsa_rows = r.json()
print(f"FMCSA returned {len(fmcsa_rows)} rows:")
for row in fmcsa_rows:
    print(f"  date={row.get('insp_date','?')}, state={row.get('report_state','?')}, "
          f"level={row.get('insp_level_id','?')}, viol={row.get('viol_total','?')}, "
          f"oos_v={row.get('oos_total','?')}, oos_d={row.get('drv_oos_total','?')}")

if not fmcsa_rows:
    print("\nNo FMCSA rows found. Cannot update. Exiting.")
    cur.close()
    conn.close()
    sys.exit(1)

# --- Step 3: match FMCSA rows to our DB rows and update ---
def normalize_state(s):
    return (s or "").strip().upper()[:2]

def normalize_level(l):
    return str(l).strip() if l else None

updates = 0
for frow in fmcsa_rows:
    raw_date = frow.get("insp_date") or frow.get("inspection_date")
    if not raw_date:
        continue
    # Parse the date (Socrata returns ISO format)
    date_str = str(raw_date)[:10]  # "YYYY-MM-DD"

    f_state = normalize_state(frow.get("report_state") or frow.get("insp_state"))
    f_level = normalize_level(frow.get("insp_level_id") or frow.get("level_id"))
    f_viol  = int(float(frow.get("viol_total") or frow.get("total_violations") or 0))

    # Find matching DB row
    for db_row in our_rows:
        db_id, db_date, db_state, db_level, db_viol, db_oos_v, db_oos_d = db_row
        if (normalize_state(db_state) == f_state and
                normalize_level(db_level) == f_level and
                (db_viol or 0) == f_viol):
            print(f"\nMatched: DB id={db_id} state={db_state} level={db_level} viol={db_viol}")
            print(f"  Setting inspection_date = {date_str}")
            cur.execute(
                "UPDATE inspections SET inspection_date = %s WHERE id = %s",
                (date_str, db_id)
            )
            updates += 1
            break
    else:
        print(f"\nNo DB match for FMCSA row: state={f_state} level={f_level} viol={f_viol} date={date_str}")

conn.commit()
cur.close()
conn.close()
print(f"\nDone. Updated {updates} inspection row(s).")
