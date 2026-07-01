"""
validate_data.py
================
Post-backfill validation for the carriers table.
Checks quality of mc_number, total_drivers, total_trucks, status, cargo_type.
Writes results to validation_report.csv.

Usage:
  python validate_data.py
"""
import os, sys, csv, logging
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

DB_URL  = os.getenv("SUPABASE_DB_URL")
OUTFILE = "validation_report.csv"

CHECKS = [
    # (check_name, query_returning_count_and_detail)
    ("total_carriers",
     "SELECT COUNT(*) FROM carriers"),

    ("mc_number_placeholder",
     "SELECT COUNT(*) FROM carriers WHERE mc_number = 'MC'"),

    ("mc_number_null",
     "SELECT COUNT(*) FROM carriers WHERE mc_number IS NULL"),

    ("mc_number_valid_format",
     "SELECT COUNT(*) FROM carriers WHERE mc_number ~ '^(MC|FF|MX)[0-9]{6}$'"),

    ("fleet_zero_both",
     "SELECT COUNT(*) FROM carriers WHERE total_drivers = 0 AND total_trucks = 0"),

    ("fleet_zero_drivers_only",
     "SELECT COUNT(*) FROM carriers WHERE total_drivers = 0 AND total_trucks > 0"),

    ("fleet_zero_trucks_only",
     "SELECT COUNT(*) FROM carriers WHERE total_trucks = 0 AND total_drivers > 0"),

    ("status_null",
     "SELECT COUNT(*) FROM carriers WHERE status IS NULL"),

    ("status_active",
     "SELECT COUNT(*) FROM carriers WHERE status = 'ACTIVE'"),

    ("status_inactive",
     "SELECT COUNT(*) FROM carriers WHERE status = 'INACTIVE'"),

    ("status_out_of_service",
     "SELECT COUNT(*) FROM carriers WHERE status = 'OUT-OF-SERVICE'"),

    ("status_not_authorized",
     "SELECT COUNT(*) FROM carriers WHERE status = 'NOT AUTHORIZED'"),

    ("status_unknown_values",
     "SELECT COUNT(*) FROM carriers WHERE status NOT IN "
     "('ACTIVE','INACTIVE','OUT-OF-SERVICE','NOT AUTHORIZED') AND status IS NOT NULL"),

    ("cargo_type_null",
     "SELECT COUNT(*) FROM carriers WHERE cargo_type IS NULL"),

    ("cargo_type_populated",
     "SELECT COUNT(*) FROM carriers WHERE cargo_type IS NOT NULL"),

    ("sample_bad_mc_numbers",
     # Returns count of mc_number values that look wrong: too short, wrong prefix, etc.
     "SELECT COUNT(*) FROM carriers "
     "WHERE mc_number IS NOT NULL AND mc_number != 'MC' "
     "  AND mc_number !~ '^(MC|FF|MX)[0-9]{6}$'"),
]

SAMPLE_QUERIES = [
    ("sample_active_with_fleet",
     """SELECT dot_number, mc_number, total_drivers, total_trucks, status, cargo_type
        FROM carriers WHERE status = 'ACTIVE' AND total_trucks > 0
        ORDER BY total_trucks DESC LIMIT 5"""),

    ("sample_remaining_bad_mc",
     """SELECT dot_number, mc_number, total_drivers, total_trucks
        FROM carriers WHERE mc_number = 'MC' LIMIT 10"""),

    ("sample_mc_format_check",
     """SELECT DISTINCT LEFT(mc_number, 2) AS prefix,
               LENGTH(mc_number) AS len,
               COUNT(*) AS cnt
        FROM carriers WHERE mc_number IS NOT NULL AND mc_number != 'MC'
        GROUP BY 1, 2 ORDER BY cnt DESC LIMIT 10"""),
]


def run_checks(cur):
    results = []
    for name, sql in CHECKS:
        cur.execute(sql)
        row = cur.fetchone()
        value = row[0] if row else None
        log.info(f"  {name}: {value:,}" if isinstance(value, int) else f"  {name}: {value}")
        results.append({"check": name, "value": value})
    return results


def run_samples(cur):
    samples = []
    for name, sql in SAMPLE_QUERIES:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        log.info(f"\n  [{name}]")
        for r in rows:
            log.info(f"    {dict(zip(cols, r))}")
        samples.append({"name": name, "columns": cols, "rows": rows})
    return samples


def write_csv(check_results, sample_results):
    with open(OUTFILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["section", "check_or_column", "value"])
        for r in check_results:
            w.writerow(["counts", r["check"], r["value"]])
        for s in sample_results:
            w.writerow([])
            w.writerow([f"sample: {s['name']}"] + s["columns"])
            for row in s["rows"]:
                w.writerow([""] + list(row))
    log.info(f"\nReport written to {OUTFILE}")


def main():
    if not DB_URL:
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    with conn.cursor() as cur:
        log.info("Running validation checks...")
        check_results = run_checks(cur)
        log.info("\nRunning sample queries...")
        sample_results = run_samples(cur)
    conn.close()

    write_csv(check_results, sample_results)

    bad_mc   = next((r["value"] for r in check_results if r["check"] == "mc_number_placeholder"), None)
    zero_both = next((r["value"] for r in check_results if r["check"] == "fleet_zero_both"), None)
    log.info(f"\nSUMMARY: mc_number='MC' remaining={bad_mc:,}  |  fleet=0/0 remaining={zero_both:,}")
    if bad_mc and bad_mc > 0:
        log.warning(f"  {bad_mc:,} carriers still have mc_number='MC' — backfill may be incomplete")
    if zero_both and zero_both > 0:
        log.warning(f"  {zero_both:,} carriers still have total_drivers=0 AND total_trucks=0")


if __name__ == "__main__":
    main()
