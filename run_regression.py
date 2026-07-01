"""
run_regression.py — Phase 0 regression suite
=============================================
Rebuilds CARRIER_FACTS for every carrier in gold_carriers.json,
diffs each field against the locked expected values, and exits
non-zero if any unexpected change is detected.

Run after every code change:
    python run_regression.py

To add a new gold carrier:  edit gold_carriers.json.
To update an expected value: edit the gold entry (document why in _notes).

Exit codes:
    0 — all gold fields matched
    1 — one or more fields changed (see report for which ones)
    2 — setup error (DB connection, missing files, etc.)

Usage:
    python run_regression.py
    python run_regression.py --verbose            # show all fields, not just diffs
    python run_regression.py --dot 204814         # single carrier
    python run_regression.py --no-halt            # print all diffs but exit 0 (CI review mode)
"""

import os, sys, json, argparse
import psycopg2
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from carrier_facts import build_carrier_facts, NOT_FOUND
    from validation_rules import run_validation
except ImportError as e:
    print(f"ERROR: Cannot import carrier_facts or validation_rules: {e}")
    print("Make sure both files are in the same directory as run_regression.py.")
    sys.exit(2)

GOLD_FILE = Path(__file__).parent / "gold_carriers.json"

# Fields from CarrierFacts that correspond to gold_carriers.json keys.
# Map: gold key → attribute name on CarrierFacts object
FIELD_MAP = {
    "legal_name":          "legal_name",
    "carrier_type":        "carrier_type",
    "usdot_status":        "usdot_status",
    "authority_required":  "authority_required",
    "authority_status":    "authority_status",
    "insurance_required":  "insurance_required",
    "insurance_status":    "insurance_status",
    "fleet_power_units":   "fleet_power_units",
    "fleet_drivers":       "fleet_drivers",
}

# Keys starting with _ are metadata — never compared
META_PREFIXES = ("_",)


def load_gold() -> dict:
    if not GOLD_FILE.exists():
        print(f"ERROR: gold_carriers.json not found at {GOLD_FILE}")
        sys.exit(2)
    with open(GOLD_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Strip top-level _meta entry
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_fact_value(facts, field: str):
    """Get a field value from CarrierFacts, returning NOT_FOUND if absent."""
    return getattr(facts, field, NOT_FOUND)


def diff_one(dot: str, gold: dict, facts) -> list[dict]:
    """
    Compare gold expected values against actual CARRIER_FACTS.
    Returns list of diffs: {field, expected, actual, match}.
    Only checks fields that are present in the gold record and are not metadata.
    """
    diffs = []
    for gold_key, gold_val in gold.items():
        # Skip metadata keys
        if any(gold_key.startswith(p) for p in META_PREFIXES):
            continue
        if gold_key not in FIELD_MAP:
            continue  # field not mapped to CarrierFacts — skip silently

        attr = FIELD_MAP[gold_key]
        actual_val = get_fact_value(facts, attr)

        # Normalise for comparison: int vs int, str vs str
        if isinstance(gold_val, int):
            try:
                actual_cmp = int(actual_val)
            except (TypeError, ValueError):
                actual_cmp = actual_val
            match = actual_cmp == gold_val
        else:
            actual_cmp = str(actual_val) if actual_val is not None else ""
            match = actual_cmp == str(gold_val)

        diffs.append({
            "field":    gold_key,
            "expected": gold_val,
            "actual":   actual_val,
            "match":    match,
        })
    return diffs


def main():
    parser = argparse.ArgumentParser(description="Carrier Facts regression suite")
    parser.add_argument("--verbose", action="store_true",
                        help="Print all field comparisons, not just mismatches")
    parser.add_argument("--dot", nargs="+", help="Check only these DOTs (subset)")
    parser.add_argument("--date", dest="accident_date",
                        help="Accident date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-halt", action="store_true",
                        help="Print diffs but exit 0 even if mismatches found")
    args = parser.parse_args()

    db_url = os.getenv("SUPABASE_DB_URL", "")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL not set in .env")
        sys.exit(2)

    gold_all = load_gold()
    if args.dot:
        gold_all = {k: v for k, v in gold_all.items() if k in args.dot}
        missing = set(args.dot) - set(gold_all.keys())
        if missing:
            print(f"WARNING: DOTs not in gold_carriers.json: {sorted(missing)}")

    try:
        conn = psycopg2.connect(db_url, connect_timeout=30)
        conn.autocommit = True
    except Exception as e:
        print(f"ERROR: DB connection failed: {e}")
        sys.exit(2)

    W = 80
    lines = []
    def p(s=""): print(s); lines.append(s)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p("=" * W)
    p(f"CARRIER FACTS REGRESSION SUITE — {ts}")
    p(f"Gold file: {GOLD_FILE.name}  |  Carriers: {len(gold_all)}")
    if args.accident_date:
        p(f"Accident date: {args.accident_date}")
    p("=" * W)

    n_carriers_checked   = 0
    n_carriers_identical = 0
    n_carriers_changed   = 0
    n_carriers_error     = 0
    total_fields         = 0
    total_matches        = 0
    all_mismatches: list[dict] = []  # {dot, name, field, expected, actual}

    for dot, gold in gold_all.items():
        name = gold.get("_name", "—")
        p(f"\n{'─' * W}")
        p(f"DOT {dot}  —  {name}")

        try:
            facts = build_carrier_facts(conn, dot, args.accident_date)
        except Exception as exc:
            p(f"  [ERROR] build_carrier_facts failed: {exc}")
            n_carriers_error += 1
            continue

        if facts.legal_name == NOT_FOUND:
            p(f"  [SKIP] DOT not found in DB")
            n_carriers_error += 1
            continue

        # Also run validation to detect any new rule failures
        vresults = run_validation(facts)
        n_vfail  = sum(1 for r in vresults if not r.passed)
        if n_vfail:
            p(f"  [WARN] {n_vfail} validation rule(s) failed — see audit_8_dots.py for details")

        diffs = diff_one(dot, gold, facts)
        n_carriers_checked += 1

        mismatches = [d for d in diffs if not d["match"]]
        matches    = [d for d in diffs if d["match"]]
        total_fields  += len(diffs)
        total_matches += len(matches)

        if not mismatches:
            n_carriers_identical += 1
            p(f"  IDENTICAL  ({len(diffs)} fields checked)")
            if args.verbose:
                for d in diffs:
                    p(f"    ✓ {d['field']:<28} {repr(d['actual'])}")
        else:
            n_carriers_changed += 1
            p(f"  CHANGED    ({len(mismatches)} of {len(diffs)} fields differ)")
            for d in mismatches:
                p(f"    ✗ {d['field']:<28} expected={repr(d['expected'])}  actual={repr(d['actual'])}")
                all_mismatches.append({
                    "dot": dot, "name": name,
                    "field": d["field"],
                    "expected": d["expected"],
                    "actual": d["actual"],
                })
            if args.verbose:
                for d in matches:
                    p(f"    ✓ {d['field']:<28} {repr(d['actual'])}")

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    pct = total_matches / total_fields * 100 if total_fields else 0
    p("")
    p("=" * W)
    p("REGRESSION SUMMARY")
    p("=" * W)
    p(f"  Carriers checked: {n_carriers_checked}")
    p(f"  Identical:        {n_carriers_identical}")
    p(f"  Changed:          {n_carriers_changed}")
    p(f"  Errors/not in DB: {n_carriers_error}")
    p(f"  Fields matched:   {total_matches}/{total_fields} ({pct:.1f}%)")

    if all_mismatches:
        p("")
        p("  MISMATCHES (investigate before release):")
        for m in all_mismatches:
            p(f"    DOT {m['dot']} {m['name'][:30]:<30}  "
              f"{m['field']}: {repr(m['expected'])} → {repr(m['actual'])}")
        p("")
        p("  To fix: if the change is an intentional improvement, update gold_carriers.json.")
        p("  If the change is a regression, revert or fix the code change.")
    else:
        p("")
        p("  All gold fields matched. No regressions detected.")

    p("=" * W)

    # Save report
    out = Path(__file__).parent / "regression_report.txt"
    with open(out, "w", encoding="utf-8") as f_out:
        f_out.write("\n".join(lines))
    print(f"\nReport saved to {out.name}")

    # Exit non-zero if any unexpected changes
    if all_mismatches and not args.no_halt:
        sys.exit(1)


if __name__ == "__main__":
    main()
