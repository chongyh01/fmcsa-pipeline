"""
fix_carrier_fields_targeted.py
Fix total_drivers, total_trucks, status, cargo_type, mc_number
for carriers that have crash records only (~714K carriers).
Much faster than fixing all 4.4M carriers.
"""
import os, time, psycopg2, requests
from psycopg2.extras import execute_values

DB_URL    = os.environ["DATABASE_URL"]
SOCRATA   = "https://data.transportation.gov/resource/az4n-8mr2.json"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")
BATCH     = 500

STATUS_MAP = {"A": "ACTIVE", "I": "INACTIVE", "X": "OUT-OF-SERVICE", "N": "NOT AUTHORIZED"}

def fetch_crash_dots(conn):
    with conn.cursor() as cur:
        # Get crash DOTs without join to avoid timeout on large tables
        cur.execute("SELECT DISTINCT dot_number FROM crashes ORDER BY dot_number")
        return [r[0] for r in cur.fetchall()]

def fetch_socrata_batch(dots):
    in_clause = ",".join(f"'{d}'" for d in dots)
    params = {
        "$where": f"dot_number in({in_clause})",
        "$limit": len(dots) + 10,
        "$select": "dot_number,total_drivers,power_units,status_code,classdef,"
                   "docket1prefix,docket1,docket2prefix,docket2,docket3prefix,docket3",
    }
    headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
    r = requests.get(SOCRATA, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

def build_row(rec):
    mc = None
    for i in ("1", "2", "3"):
        prefix = (rec.get(f"docket{i}prefix") or "").strip()
        number = (rec.get(f"docket{i}") or "").strip()
        if prefix and number:
            mc = f"{prefix}{number}"
            break
    status_raw = (rec.get("status_code") or "").strip() or None
    return (
        rec.get("dot_number"),
        int(rec.get("total_drivers") or 0),
        int(rec.get("power_units") or 0),
        STATUS_MAP.get(status_raw, status_raw),
        (rec.get("classdef") or "").strip() or None,
        mc,
    )

def update_batch(conn, rows):
    with conn.cursor() as cur:
        execute_values(cur, """
            UPDATE carriers SET
                total_drivers = d.total_drivers,
                total_trucks  = d.total_trucks,
                status        = d.status,
                cargo_type    = d.cargo_type,
                mc_number     = COALESCE(d.mc_number, carriers.mc_number)
            FROM (VALUES %s) AS d(dot_number, total_drivers, total_trucks, status, cargo_type, mc_number)
            WHERE carriers.dot_number = d.dot_number
        """, rows, template="(%s, %s, %s, %s, %s, %s)")
    conn.commit()

def main():
    conn = psycopg2.connect(DB_URL)
    dots = fetch_crash_dots(conn)
    print(f"Crash carriers to fix: {len(dots):,}")

    start = time.time()
    updated = 0
    for i in range(0, len(dots), BATCH):
        chunk = dots[i:i + BATCH]
        try:
            records = fetch_socrata_batch(chunk)
        except Exception as e:
            print(f"  Batch {i}: Socrata error — {e}, skipping")
            time.sleep(2)
            continue

        rows = [build_row(r) for r in records if r.get("dot_number")]
        if rows:
            update_batch(conn, rows)
            updated += len(rows)

        done = i + BATCH
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 1
        remaining = (len(dots) - done) / rate if rate > 0 else 0
        print(f"  {min(done, len(dots)):,}/{len(dots):,} — {updated:,} updated — "
              f"~{remaining/60:.0f}min remaining")
        time.sleep(0.15)

    conn.close()
    print(f"Done. {updated:,} carriers updated in {(time.time()-start)/60:.1f} min.")

if __name__ == "__main__":
    main()
