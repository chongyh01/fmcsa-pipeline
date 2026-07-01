"""
audit_8_dots.py
===============
Runs Layer 1 (carrier_facts.py) + Layer 2 (validation_rules.py) over the
8 audit DOTs from REPORT_PIPELINE_REBUILD.md and prints a pass/fail report.

Usage:
    python audit_8_dots.py
    python audit_8_dots.py --dot 204814 3431540     # specific subset
    python audit_8_dots.py --date 2026-01-15        # with accident date filter
    python audit_8_dots.py --verbose                # show full facts object
    python audit_8_dots.py --failures-only          # show only failing rules

Output: printed report + saved to audit_8_dots_report.txt
"""

import os, sys, argparse
import psycopg2
from dotenv import load_dotenv
from datetime import datetime

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from carrier_facts import build_carrier_facts, NOT_FOUND
from validation_rules import run_validation

# ── Audit DOTs from REPORT_PIPELINE_REBUILD.md ───────────────────────────────
AUDIT_DOTS = [
    ("204814",  "BINKS COCA COLA BOTTLING",
     "14 trucks/13 drivers — fleet should both be non-zero"),
    ("3431540", "YAFET TRUCKING",
     "inspection count: report said 12, SMS ~9 — verify dedup gives correct count"),
    ("3612954", "AMPF LOGISTICS",
     "active vs revoked + no insurance contradiction — rules 1, 6, 7 must all pass"),
    ("3841767", "ABLE BODY LOGISTICS",
     "summary inactive vs findings active — usdot_status must be consistent"),
    ("623336",  "CARBARB ENTERPRISES",
     "revoked vs active conflict — rules 1 and 7 must catch contradiction if present"),
    ("3012101", "XIANGFENG TRADING",
     "private, 1/1 — must classify as PRIVATE, not FOR_HIRE_INTERSTATE (rules 2, 3)"),
    ("1612145", "J8 EQUIPMENT",
     "private property 1/1 — same as above, rules 2 and 3"),
    ("2308088", "JEREMIAH BROOKS",
     "for-hire wording vs service/intrastate — carrier_type must match evidence (rule 19)"),
]


# ── Formatting helpers ────────────────────────────────────────────────────────

W = 80

def divider(ch="─"): return ch * W

def print_facts(facts, lines, verbose=False):
    def p(s=""): print(s); lines.append(s)

    rows = [
        ("carrier_type",           facts.carrier_type),
        ("usdot_status",           facts.usdot_status),
        ("mc_number",              facts.mc_number or "(none)"),
        ("authority_required",     facts.authority_required),
        ("authority_status",       facts.authority_status),
        ("authority_revoc_date",   facts.authority_revocation_date or "—"),
        ("first_authority_date",   facts.first_authority_date      or "—"),
        ("active_auth_period",     facts.active_authority_period   or "—"),
        ("insurance_required",     facts.insurance_required),
        ("insurance_status",       facts.insurance_status),
        ("insurance_cancellation", facts.insurance_cancellation),
        ("insurance_replacement",  facts.insurance_replacement),
        ("fleet_power_units",      str(facts.fleet_power_units)),
        ("fleet_non_cmv_units",    str(facts.fleet_non_cmv_units)),
        ("fleet_drivers",          str(facts.fleet_drivers)),
        ("has_passenger_cargo",    str(facts.has_passenger_cargo)),
        ("inspection_count",       str(facts.inspection_count) + " (deduped)"),
        ("crash_count",            str(facts.crash_count)),
        ("violation_count",        str(facts.violation_count)),
        ("boc3_on_file",           facts.boc3_on_file),
        ("sms_percentile",         str(facts.sms_percentile_present)),
        ("accident_date",          facts.accident_date or "(none)"),
        ("data_confidence",        str(facts.data_confidence)),
    ]

    if verbose:
        rows += [
            ("_auth_records",  str(len(facts._authority_records)) + " rows"),
            ("_ins_records",   str(len(facts._insurance_records)) + " rows"),
            ("_alerts",        str(len(facts._alerts))            + " rows"),
        ]

    for k, v in rows:
        p(f"  {k:<26} {v}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audit 8 carrier DOTs through Layer 1 + 2")
    parser.add_argument("--dot", nargs="+", help="Specific DOT numbers to check")
    parser.add_argument("--date", dest="accident_date", help="Accident date YYYY-MM-DD")
    parser.add_argument("--verbose", action="store_true", help="Print raw record counts too")
    parser.add_argument("--failures-only", action="store_true",
                        help="Only print failed rules (not N/A or PASS)")
    args = parser.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL", "")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL not set in .env"); sys.exit(1)

    conn = psycopg2.connect(db_url, connect_timeout=30)
    conn.autocommit = True

    lines = []
    def p(s=""): print(s); lines.append(s)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p("=" * W)
    p(f"CARRIER FACTS + VALIDATION AUDIT — {ts}")
    if args.accident_date:
        p(f"Accident date filter: {args.accident_date}")
    p("Layers: 1 (carrier_facts.py)  +  2 (validation_rules.py)")
    p("=" * W)

    dot_meta    = {d[0]: (d[1], d[2]) for d in AUDIT_DOTS}
    dots_to_run = args.dot if args.dot else [d[0] for d in AUDIT_DOTS]

    summary_rows = []
    total_rules = total_pass = total_fail = 0

    for dot in dots_to_run:
        name, audit_note = dot_meta.get(dot, ("UNKNOWN", "not in audit list"))

        p("")
        p(divider())
        p(f"DOT {dot}  —  {name}")
        p(f"Note: {audit_note}")

        # ── Layer 1: build facts ──────────────────────────────────────────────
        try:
            facts = build_carrier_facts(conn, dot, args.accident_date)
        except Exception as exc:
            p(f"\n  [ERROR] build_carrier_facts failed: {exc}")
            summary_rows.append((dot, name, -1, -1, "BUILD_ERROR"))
            continue

        if facts.legal_name == NOT_FOUND:
            p(f"\n  [WARNING] DOT {dot} not found in DB — skipping")
            summary_rows.append((dot, name, -1, -1, "NOT_IN_DB"))
            continue

        p(f"\n  [LAYER 1 — CARRIER_FACTS]")
        print_facts(facts, lines, verbose=args.verbose)

        # ── Layer 2: run validation ───────────────────────────────────────────
        results = run_validation(facts)
        n_pass  = sum(1 for r in results if r.passed)
        n_fail  = sum(1 for r in results if not r.passed)
        total_rules += len(results)
        total_pass  += n_pass
        total_fail  += n_fail

        p(f"\n  [LAYER 2 — VALIDATION]  {n_pass}/{len(results)} rules passed"
          f"{'  ✓' if n_fail == 0 else f'  — {n_fail} FAILED'}")

        for r in results:
            if args.failures_only and r.passed:
                continue  # skip passing and N/A lines in failures-only mode
            p(f"    {r}")

        summary_rows.append((dot, name, n_pass, n_fail, "OK" if n_fail == 0 else "FAIL"))

    conn.close()

    # ── Summary table ─────────────────────────────────────────────────────────
    p("")
    p("=" * W)
    p("SUMMARY")
    p("=" * W)
    p(f"  {'DOT':<10} {'Pass':>5} {'Fail':>5}  {'Status':<14} Name")
    p("  " + divider("-"))
    for dot, name, n_pass, n_fail, status in summary_rows:
        if status in ("BUILD_ERROR", "NOT_IN_DB"):
            p(f"  {dot:<10} {'—':>5} {'—':>5}  {status:<14} {name}")
        else:
            p(f"  {dot:<10} {n_pass:>5} {n_fail:>5}  {status:<14} {name}")

    pct = total_pass / total_rules * 100 if total_rules else 0
    p("")
    p(f"  Total: {total_pass}/{total_rules} rules passed ({pct:.1f}%) | {total_fail} failures")
    p("=" * W)
    p("PASS = all 20 rules green  |  FAIL = one or more rules failed")
    p("N/A rules count as PASS (rule does not apply to this carrier type)")
    p("")

    out = os.path.join(os.path.dirname(__file__), "audit_8_dots_report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Report saved to {out}")


if __name__ == "__main__":
    main()
