"""
_deep_verify.py
===============
Investigates specific badge issues found in verify_badges.py:
1. BINKS Coca Cola (204814) — 5 crashes inc. 1 fatal — are they real?
2. Find correct DOT for "TRUCKING R US LLC"
3. Buckshot (2259497) revocation details
4. Cross-check key carriers via Socrata az4n-8mr2
"""
import os, sys, requests, time
import psycopg2, psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URL    = os.getenv("SUPABASE_DB_URL")
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
HEADERS   = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
CARRIER_DS = "az4n-8mr2"


def socrata_carrier(dot):
    dot_padded = str(dot).zfill(8)
    url = f"https://data.transportation.gov/resource/{CARRIER_DS}.json"
    try:
        r = requests.get(url, params={
            "$where": f"dot_number='{dot_padded}'",
            "$limit": 1,
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        return {"error": str(e)}


def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.cursor_factory = psycopg2.extras.RealDictCursor

    with conn.cursor() as cur:

        # ── 1. BINKS crash detail ──────────────────────────────────────────
        print("=" * 65)
        print("1. BINKS Coca Cola DOT 204814 — crash detail")
        print("=" * 65)
        cur.execute("""
            SELECT crash_date, state, fatal, injury, towaway, report_number
            FROM crashes WHERE dot_number = '204814'
            ORDER BY crash_date DESC
        """)
        rows = cur.fetchall()
        if not rows:
            print("  No crashes in DB")
        for r in rows:
            print(f"  {r['crash_date']}  state={r['state']}  fatal={r['fatal']}"
                  f"  injury={r['injury']}  towaway={r['towaway']}  rpt={r['report_number']}")

        print("\n  Socrata cross-check for DOT 204814...")
        soc = socrata_carrier("204814")
        if soc and "error" not in soc:
            print(f"  legal_name: {soc.get('legal_name')}")
            print(f"  status: {soc.get('entity_type')} / common_auth={soc.get('common_authority')}")
            print(f"  cargo_type: {soc.get('cargo_carried')} / private_check={soc.get('private_check')}")
            print(f"  total_crashes (SAFER census): {soc.get('crash_total')}")
            print(f"  fatal_crashes (SAFER census): {soc.get('crash_fatal')}")
        elif soc:
            print(f"  Socrata error: {soc.get('error')}")

        # ── 2. Find TRUCKING R US LLC ─────────────────────────────────────
        print("\n" + "=" * 65)
        print("2. Finding 'TRUCKING R US LLC' in DB")
        print("=" * 65)
        cur.execute("""
            SELECT dot_number, legal_name, status
            FROM carriers
            WHERE legal_name ILIKE '%%TRUCKING R US%%'
            LIMIT 10
        """)
        rows = cur.fetchall()
        if not rows:
            print("  Not found in DB")
        for r in rows:
            print(f"  DOT {r['dot_number']}  {r['legal_name']}  status={r['status']}")

        # ── 3. Buckshot revocation detail ─────────────────────────────────
        print("\n" + "=" * 65)
        print("3. Buckshot Transportation DOT 2259497 — revocation detail")
        print("=" * 65)
        cur.execute("""
            SELECT event_type, event_date, description, source_file
            FROM carrier_alerts
            WHERE dot_number = '2259497'
            ORDER BY event_date
        """)
        rows = cur.fetchall()
        for r in rows:
            print(f"  {r['event_type']}  {r['event_date']}  {r['description']}  src={r['source_file']}")
        if not rows:
            print("  No carrier_alerts rows")

        cur.execute("""
            SELECT authority_type, status, effective_date, revocation_date, reason
            FROM authority_history
            WHERE dot_number = '2259497'
            ORDER BY effective_date
        """)
        rows = cur.fetchall()
        print(f"\n  authority_history ({len(rows)} rows):")
        for r in rows:
            print(f"  {r['status']}  eff={r['effective_date']}  rev={r['revocation_date']}  reason={r['reason']}")

        # ── 4. Cross-check all 4 badge carriers against Socrata ───────────
        print("\n" + "=" * 65)
        print("4. Socrata cross-check — status for key carriers")
        print("=" * 65)
        for dot, label in [
            ("2293690", "Bob Hammer / Nu Breed"),
            ("204814",  "BINKS Coca Cola"),
            ("2259497", "Buckshot Transportation"),
            ("85526",   "Exhibitor's Service Co"),
            ("100115",  "Lamers Bus Lines"),
        ]:
            soc = socrata_carrier(dot)
            if soc and "error" not in soc:
                ca = soc.get("common_authority", "?")
                st = soc.get("out_of_service", "?")
                nm = soc.get("legal_name", "?")
                cr = soc.get("crash_total", "?")
                cf = soc.get("crash_fatal", "?")
                print(f"  DOT {dot}  {label}")
                print(f"    FMCSA name={nm}  common_auth={ca}  out_of_service={st}")
                print(f"    crash_total={cr}  crash_fatal={cf}")
            else:
                err = soc.get("error") if soc else "No data"
                print(f"  DOT {dot}  {label}  → {err}")
            time.sleep(0.3)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
