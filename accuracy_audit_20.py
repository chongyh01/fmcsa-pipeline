"""
Accuracy audit: 20 random ACTIVE carriers — DB vs Socrata comparison.
"""

import os
import sys
import psycopg2
import requests
import json
from dotenv import load_dotenv

# Load env
env_path = r"C:\Users\chong\OneDrive\Documents\Desktop\MISC PROJECT\US DIRECTORY\CARRIER INTELLIGENT REPORT\5 Jun 26 - CARRIER PORTAL\CODES\.env"
load_dotenv(env_path)

SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]
SOCRATA_APP_TOKEN = os.environ["SOCRATA_APP_TOKEN"]
SOCRATA_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"

# ── 1. Pull 20 random ACTIVE carriers from DB ─────────────────────────────────
print("Pulling 20 random ACTIVE carriers from DB...")
conn = psycopg2.connect(SUPABASE_DB_URL)
cur = conn.cursor()
cur.execute("""
    SELECT dot_number, legal_name, mc_number, total_drivers, total_trucks, status, cargo_type
    FROM carriers
    WHERE status = 'ACTIVE'
    ORDER BY RANDOM()
    LIMIT 20
""")
rows = cur.fetchall()
cols = ["dot_number", "legal_name", "mc_number", "total_drivers", "total_trucks", "status", "cargo_type"]
carriers = [dict(zip(cols, r)) for r in rows]
cur.close()
conn.close()
print(f"Got {len(carriers)} carriers.\n")

# ── 2. Fetch each from Socrata and compare ────────────────────────────────────
results = []
first_raw = None

for idx, c in enumerate(carriers):
    dot = str(c["dot_number"]).zfill(8)
    resp = requests.get(
        SOCRATA_URL,
        params={"dot_number": dot, "$limit": 1},
        headers={"X-App-Token": SOCRATA_APP_TOKEN},
        timeout=15,
    )
    if resp.status_code != 200 or not resp.json():
        results.append({
            "dot_number": c["dot_number"],
            "our_legal_name": c["legal_name"],
            "error": f"HTTP {resp.status_code} or empty response",
        })
        continue

    s = resp.json()[0]
    if first_raw is None:
        first_raw = s

    # ── Field comparisons ──────────────────────────────────────────────────────
    # legal_name (case-insensitive, trimmed)
    our_name = (c["legal_name"] or "").strip().upper()
    soc_name = (s.get("legal_name", "") or "").strip().upper()
    name_match = our_name == soc_name

    # status: our ACTIVE vs Socrata status_code ('A'=Active, 'I'=Inactive, etc.)
    soc_status_raw = s.get("status_code", "")
    soc_status_upper = str(soc_status_raw).strip().upper()
    # 'A' = Active in Socrata; we store 'ACTIVE'
    if soc_status_raw == "":
        soc_status_display = "[field missing in Socrata]"
        status_match = None
    else:
        soc_status_display = soc_status_raw
        status_match = (soc_status_upper == "A")  # we expect ACTIVE carriers → 'A'

    # total_drivers
    try:
        soc_drivers = int(s.get("total_drivers") or 0)
    except:
        soc_drivers = None
    our_drivers = c["total_drivers"]
    drivers_match = (soc_drivers is not None) and (our_drivers == soc_drivers)

    # total_trucks (Socrata: power_units — this dataset uses 'power_units' not 'total_power_units')
    try:
        soc_trucks = int(s.get("power_units") or 0)
    except:
        soc_trucks = None
    our_trucks = c["total_trucks"]
    trucks_match = (soc_trucks is not None) and (our_trucks == soc_trucks)

    # mc_number format: our "MC000074" vs Socrata's docket_number "MC000074"
    our_mc = (c["mc_number"] or "").strip().upper()
    soc_docket = (s.get("docket_number") or "").strip().upper()
    # Normalise: strip leading zeros after "MC"
    def norm_mc(val):
        if not val:
            return ""
        prefix = ""
        for p in ("MC", "FF", "MX"):
            if val.startswith(p):
                prefix = p
                num_part = val[len(p):]
                return prefix + str(int(num_part)) if num_part.isdigit() else val
        return val
    mc_match = norm_mc(our_mc) == norm_mc(soc_docket)

    result = {
        "dot_number": c["dot_number"],
        "our_legal_name": c["legal_name"],
        "soc_legal_name": s.get("legal_name", ""),
        "name_match": name_match,
        "our_status": c["status"],
        "soc_status": soc_status_display,
        "status_match": status_match,
        "our_drivers": our_drivers,
        "soc_drivers": soc_drivers,
        "drivers_match": drivers_match,
        "our_trucks": our_trucks,
        "soc_trucks": soc_trucks,
        "trucks_match": trucks_match,
        "our_mc": our_mc,
        "soc_docket": soc_docket,
        "mc_match": mc_match,
        "all_match": all([name_match, status_match is not False, drivers_match, trucks_match, mc_match]),
    }
    results.append(result)
    print(f"DOT {c['dot_number']:>8} | name={'OK' if name_match else 'MISMATCH':8} | "
          f"drivers={'OK' if drivers_match else 'MISMATCH':8} | trucks={'OK' if trucks_match else 'MISMATCH':8} | "
          f"mc={'OK' if mc_match else 'MISMATCH':8}")

# ── 3. Summary report ─────────────────────────────────────────────────────────
print("\n" + "="*80)
print("ACCURACY AUDIT — 20 RANDOM ACTIVE CARRIERS")
print("="*80)

total = len([r for r in results if "error" not in r])
errors = [r for r in results if "error" in r]

if errors:
    print(f"\nSocrata fetch errors ({len(errors)}):")
    for e in errors:
        print(f"  DOT {e['dot_number']}: {e['error']}")

name_ok    = sum(1 for r in results if r.get("name_match") is True)
status_ok  = sum(1 for r in results if r.get("status_match") is True)
drivers_ok = sum(1 for r in results if r.get("drivers_match") is True)
trucks_ok  = sum(1 for r in results if r.get("trucks_match") is True)
mc_ok      = sum(1 for r in results if r.get("mc_match") is True)
all_ok     = sum(1 for r in results if r.get("all_match") is True)

print(f"\nCarriers with Socrata data: {total}/20")
print(f"\nField-level accuracy (out of {total} matched):")
print(f"  legal_name    : {name_ok}/{total}  ({100*name_ok/total:.0f}%)")
print(f"  status        : {status_ok}/{total}  ({100*status_ok/total:.0f}%)")
print(f"  total_drivers : {drivers_ok}/{total}  ({100*drivers_ok/total:.0f}%)")
print(f"  total_trucks  : {trucks_ok}/{total}  ({100*trucks_ok/total:.0f}%)")
print(f"  mc_number     : {mc_ok}/{total}  ({100*mc_ok/total:.0f}%)")
print(f"\nAll-fields match: {all_ok}/{total}  ({100*all_ok/total:.0f}%)")

# ── 4. Mismatches detail ──────────────────────────────────────────────────────
mismatches = [r for r in results if "error" not in r and not r.get("all_match")]
if mismatches:
    print(f"\nMISMATCH DETAIL ({len(mismatches)} carriers):")
    print("-"*80)
    for r in mismatches:
        print(f"\nDOT {r['dot_number']}:")
        if not r["name_match"]:
            print(f"  legal_name    DB : {r['our_legal_name']}")
            print(f"  legal_name    SC : {r['soc_legal_name']}")
        if r["status_match"] is False:
            print(f"  status        DB : {r['our_status']}")
            print(f"  status        SC : {r['soc_status']}")
        if not r["drivers_match"]:
            print(f"  total_drivers DB : {r['our_drivers']}")
            print(f"  total_drivers SC : {r['soc_drivers']}")
        if not r["trucks_match"]:
            print(f"  total_trucks  DB : {r['our_trucks']}")
            print(f"  total_trucks  SC : {r['soc_trucks']}")
        if not r["mc_match"]:
            print(f"  mc_number     DB : {r['our_mc']}")
            print(f"  docket_number SC : {r['soc_docket']}")
else:
    print("\nAll 20 carriers matched on all fields.")

# ── 5. Systematic patterns ────────────────────────────────────────────────────
print("\nSYSTEMATIC PATTERNS:")
drivers_deltas = [abs((r["our_drivers"] or 0) - (r["soc_drivers"] or 0))
                  for r in results if "error" not in r and r["soc_drivers"] is not None]
trucks_deltas  = [abs((r["our_trucks"] or 0) - (r["soc_trucks"] or 0))
                  for r in results if "error" not in r and r["soc_trucks"] is not None]
if drivers_deltas:
    print(f"  Driver delta   avg={sum(drivers_deltas)/len(drivers_deltas):.1f}  max={max(drivers_deltas)}")
if trucks_deltas:
    print(f"  Truck delta    avg={sum(trucks_deltas)/len(trucks_deltas):.1f}  max={max(trucks_deltas)}")

# Check if trucks mismatches are always DB<Socrata or DB>Socrata
trucks_db_higher = sum(1 for r in results if "error" not in r
                       and r["soc_trucks"] is not None
                       and (r["our_trucks"] or 0) > (r["soc_trucks"] or 0))
trucks_soc_higher = sum(1 for r in results if "error" not in r
                        and r["soc_trucks"] is not None
                        and (r["soc_trucks"] or 0) > (r["our_trucks"] or 0))
print(f"  Trucks DB>Socrata: {trucks_db_higher}  |  Trucks Socrata>DB: {trucks_soc_higher}")

# Dump full Socrata keys for first result (debug)
print("\nSocrata fields available (from first matched carrier):")
if first_raw:
    for k, v in first_raw.items():
        print(f"  {k}: {repr(v)}")

print("\nDone.")
