"""
verify_badges.py
================
Replicates the TypeScript badge logic in Python and queries the DB directly
to verify what badge each test carrier would get. Also cross-checks key fields
against the Socrata FMCSA API (SAFER replacement).

Test carriers:
  DOT 2293690  — Bob Hammer / Nu Breed       → expect REVOKED
  DOT 204814   — BINKS Coca Cola             → expect CLEAR (private carrier)
  DOT 1438     — TRUCKING R US LLC           → expect ELEVATED / HIGH RISK (with date)
  DOT 2259497  — Buckshot Transportation     → expect CLEAR or ELEVATED
  DOT 85526    — (general spot-check)
  DOT 100115   — (general spot-check)
  DOT 914218   — (general spot-check)
  DOT 228442   — A P Giesbrecht Trucking     → spot-check
  DOT 1234567  — synthetic non-existent (sanity check)
"""

import os, sys, json, requests, time
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URL    = os.getenv("SUPABASE_DB_URL")
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
HEADERS   = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

BADGE_DOTS = [
    ("2293690", "Bob Hammer / Nu Breed Transport",   "REVOKED"),
    ("204814",  "BINKS Coca Cola Bottling",           "CLEAR"),
    ("1438",    "Trucking R Us LLC",                  "ELEVATED or HIGH RISK (with accident date)"),
    ("2259497", "Buckshot Transportation",            "CLEAR or ELEVATED"),
    ("85526",   "General spot-check A",               "?"),
    ("100115",  "General spot-check B",               "?"),
    ("914218",  "General spot-check C",               "?"),
    ("228442",  "A P Giesbrecht Trucking",            "?"),
]

# Replicate the TypeScript getRiskLevel logic (search-page version, no accident date)
def get_risk_level(carrier_row, revocations, crashes, sms_rows):
    status = (carrier_row.get("status") or "").upper()
    is_inactive = status in ("NOT AUTHORIZED", "INACTIVE", "")

    # Has any real revocation (not DISCONTINUED)
    real_revocations = [
        r for r in revocations
        if "DISCONTINUED" not in (r.get("reason") or "").upper()
        and "DISCONTINUED" not in (r.get("revocation_type") or "").upper()
    ]
    has_revocation_history = len(real_revocations) > 0

    if is_inactive and has_revocation_history:
        return "REVOKED"

    fatal_crashes   = [c for c in crashes if (c.get("fatal") or 0) > 0]
    sms_alert_count = len(sms_rows)
    has_crashes     = len(crashes) > 0
    has_sms         = sms_alert_count > 0

    if fatal_crashes or sms_alert_count >= 3:
        return "HIGH RISK"
    if has_crashes or has_sms:
        return "ELEVATED"
    if not is_inactive and has_revocation_history:
        return "ACTIVE — PRIOR HISTORY"
    if not is_inactive:
        return "CLEAR"
    return "INACTIVE"


def fetch_carrier_data(cur, dot):
    # 1. Carrier row
    cur.execute("SELECT * FROM carriers WHERE dot_number = %s", (dot,))
    carrier = cur.fetchone()
    if not carrier:
        return None, [], [], []

    # 2. Revocations from carrier_alerts (INVOLUNTARY_REVOCATION)
    cur.execute("""
        SELECT event_type, event_date, description
        FROM carrier_alerts
        WHERE dot_number = %s AND event_type = 'INVOLUNTARY_REVOCATION'
        LIMIT 20
    """, (dot,))
    alerts = cur.fetchall()

    # 3. Revocations from authority_history
    cur.execute("""
        SELECT status, effective_date, reason
        FROM authority_history
        WHERE dot_number = %s
          AND status ILIKE '%%INVOLUNTARY%%'
          AND status NOT ILIKE '%%DISCONTINUED%%'
        LIMIT 20
    """, (dot,))
    ah_revs = cur.fetchall()

    all_revocations = [
        {"revocation_type": r["event_type"], "reason": r["description"]} for r in alerts
    ] + [
        {"reason": r["reason"], "revocation_type": r["status"]} for r in ah_revs
    ]

    # 4. Crashes (column is 'fatal', not 'fatalities')
    cur.execute("""
        SELECT fatal, injury, towaway, report_number, crash_date
        FROM crashes WHERE dot_number = %s LIMIT 500
    """, (dot,))
    crashes = [dict(r) for r in cur.fetchall()]

    # 5. SMS BASIC alert flags from sms_scores (count TRUE alert columns)
    cur.execute("""
        SELECT unsafe_driving_alert, hours_of_service_compliance_alert,
               driver_fitness_alert, controlled_substances_alcohol_alert,
               vehicle_maintenance_alert, hazardous_materials_alert, crash_indicator_alert
        FROM sms_scores WHERE dot_number = %s
        ORDER BY score_date DESC LIMIT 1
    """, (dot,))
    sms_row = cur.fetchone()
    sms_alerts = []
    if sms_row:
        alert_cols = [
            "unsafe_driving_alert", "hours_of_service_compliance_alert",
            "driver_fitness_alert", "controlled_substances_alcohol_alert",
            "vehicle_maintenance_alert", "hazardous_materials_alert", "crash_indicator_alert"
        ]
        sms_alerts = [c for c in alert_cols if sms_row.get(c)]

    return dict(carrier), all_revocations, crashes, sms_alerts


def fetch_socrata_carrier(dot):
    """Query Socrata Carrier All With History dataset for a DOT number."""
    dot_padded = str(dot).zfill(8)
    url = "https://data.transportation.gov/resource/7lisa-hiej.csv"
    try:
        r = requests.get(url, params={
            "$where": f"usdot_number='{dot_padded}'",
            "$limit": 1,
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        lines = r.text.strip().split("\n")
        if len(lines) < 2:
            return None
        headers = lines[0].split(",")
        vals    = lines[1].split(",")
        return dict(zip(headers, vals))
    except Exception as e:
        return {"error": str(e)}


def fetch_socrata_auth(dot):
    """Query authority history for a carrier."""
    dot_padded = str(dot).zfill(8)
    url = "https://data.transportation.gov/resource/2khk-epam.csv"
    try:
        r = requests.get(url, params={
            "$where": f"usdot_number='{dot_padded}'",
            "$limit": 20,
            "$order": "served_date DESC",
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        import io, csv
        reader = csv.DictReader(io.StringIO(r.text))
        return list(reader)
    except Exception as e:
        return [{"error": str(e)}]


def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.cursor_factory = psycopg2.extras.RealDictCursor

    print("=" * 70)
    print("BADGE VERIFICATION REPORT")
    print(f"Run date: {date.today()} SGT")
    print("=" * 70)

    with conn.cursor() as cur:
        for dot, label, expected in BADGE_DOTS:
            print(f"\n{'─'*70}")
            print(f"DOT {dot}  |  {label}")
            print(f"Expected badge: {expected}")

            carrier, revocations, crashes, sms = fetch_carrier_data(cur, dot)
            if not carrier:
                print("  ⚠ NOT FOUND in DB")
                continue

            badge = get_risk_level(carrier, revocations, crashes, sms)

            status      = carrier.get("status", "?")
            legal_name  = carrier.get("legal_name", "?")
            crash_count = len(crashes)
            fatal_count = sum(1 for c in crashes if (c.get("fatal") or 0) > 0)
            rev_count   = len([r for r in revocations
                               if "DISCONTINUED" not in (r.get("reason") or "").upper()
                               and "DISCONTINUED" not in (r.get("revocation_type") or "").upper()])
            sms_count   = len(sms)

            print(f"  DB name:   {legal_name}")
            print(f"  Status:    {status}")
            print(f"  Revocations (non-DISCONTINUED): {rev_count}")
            print(f"  Crashes total: {crash_count}  (fatal: {fatal_count})")
            print(f"  OOS orders: {sms_count}")
            print(f"  → COMPUTED BADGE: {badge}")

            ok = "✅" if expected.startswith(badge) or expected == "?" else "❌ MISMATCH"
            if expected != "?":
                print(f"  {ok}  (expected: {expected})")

            # Socrata cross-check
            print(f"  Fetching Socrata for cross-check...", end=" ", flush=True)
            soc = fetch_socrata_carrier(dot)
            if soc and "error" not in soc:
                soc_name    = soc.get("legal_name", soc.get('"legal_name"', "?"))
                common_auth = soc.get("common_authority", "?")
                print(f"OK")
                print(f"  Socrata name: {soc_name}  |  common_authority: {common_auth}")
            elif soc and "error" in soc:
                print(f"ERROR: {soc['error']}")
            else:
                print("No data")
            time.sleep(0.5)

    conn.close()
    print(f"\n{'='*70}")
    print("Done.")


if __name__ == "__main__":
    main()
