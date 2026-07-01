"""
fix_carrier_fields.py
Backfill total_drivers, total_trucks, status, cargo_type, mc_number
for all carriers in our DB by re-fetching from FMCSA Socrata (az4n-8mr2).

Processes in batches of 1000 DOT numbers at a time using Socrata $where IN clause.
"""
import os, time, psycopg2, requests
from psycopg2.extras import execute_values

DB_URL   = os.environ["DATABASE_URL"]
SOCRATA  = "https://data.transportation.gov/resource/az4n-8mr2.json"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")  # optional but avoids throttle
BATCH    = 500

STATUS_MAP = {"A": "ACTIVE", "I": "INACTIVE", "X": "OUT-OF-SERVICE", "N": "NOT AUTHORIZED"}

def fetch_dots(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT dot_number FROM carriers ORDER BY dot_number")
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
        prefix = rec.get(f"docket{i}prefix", "").strip()
        number = rec.get(f"docket{i}", "").strip()
        if prefix and number:
            mc = f"{prefix}{number}"
            break
    status_raw = rec.get("status_code", "").strip() or None
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
    dots = fetch_dots(conn)
    print(f"Total carriers to update: {len(dots)}")

    updated = 0
    for i in range(0, len(dots), BATCH):
        chunk = dots[i:i + BATCH]
        try:
            records = fetch_socrata_batch(chunk)
        except Exception as e:
            print(f"  Batch {i}-{i+BATCH}: Socrata error — {e}, skipping")
            time.sleep(2)
            continue

        rows = [build_row(r) for r in records if r.get("dot_number")]
        if rows:
            update_batch(conn, rows)
            updated += len(rows)

        pct = min(100, (i + BATCH) / len(dots) * 100)
        print(f"  {i+BATCH}/{len(dots)} ({pct:.1f}%) — {updated} updated so far")
        time.sleep(0.2)  # gentle throttle

    conn.close()
    print(f"Done. {updated} carriers updated.")

if __name__ == "__main__":
    main()
