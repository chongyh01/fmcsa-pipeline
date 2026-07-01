"""
_generate_verification_batch.py
================================
Post-fix verification batch — 30 carrier reports.

Mandated DOTs (bug-fix verification):
  2051387  TREDZ CENTRAL LLC           — Bug 1: carrier type misclassification
  888283   PUBLIC SERVICE CO OF NC     — Bug 1: carrier type misclassification
  1074419  LDI TRUCKING INC            — Bug 2: authority/revocation date mixing

27 additional DOTs sampled for diversity:
  - Active for-hire w/ authority, revoked/lapsed, private/no-MC
  - At least 2-3 that produce validation_conflicts
  - Range of fleet sizes (owner-op → large)
  - At least one with null SMS data

Excludes:
  - 8 gold_carriers.json DOTs
  - 30 DOTs from "Carrier Report 1st July 2026" (first audit batch)

Output:
  CODES/02 - Carrier Report 1st July 2026/DOT_<number>.txt
  CODES/02 - Carrier Report 1st July 2026/manifest.csv

Usage:
    python _generate_verification_batch.py
"""

import os, sys, csv, time, textwrap
import psycopg2
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from carrier_facts import build_carrier_facts, NOT_FOUND, CONFIRMED_REVOKED
from validation_rules import run_validation_with_conflicts

DB_URL = os.getenv("SUPABASE_DB_URL", "")
if not DB_URL:
    print("ERROR: SUPABASE_DB_URL not set"); sys.exit(1)

OUTPUT_DIR = Path(__file__).parent / "02 - Carrier Report 1st July 2026"
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Exclusion sets ─────────────────────────────────────────────────────────────

GOLD_DOTS = {
    "204814", "3431540", "3612954", "3841767",
    "623336", "3012101", "1612145", "2308088",
}

PREV_BATCH_DOTS = {
    "2898056","675795","539473","488677","1302916","1372987","3411477","3384102",
    "3105097","3933118","4021252","4080399","1528750","1732647","3496197","4076555",
    "611058","2933699","2069221","1810135","4512553","2041810","1788189","713937",
    "919057","987933","1181967","2719904","2615378","4046894",
}

# 3 mandated verification DOTs (kept even though 1074419 was not in prev batch)
MANDATED = ["2051387", "888283", "1074419"]

EXCLUDE = GOLD_DOTS | PREV_BATCH_DOTS | set(MANDATED)

W = 80


# ── Sampling ──────────────────────────────────────────────────────────────────

def sample_27(conn) -> list[str]:
    ex = "(" + ",".join(f"'{d}'" for d in EXCLUDE) + ")"
    strata = [
        # Active FOR_HIRE, small-medium fleet, no conflicts (baseline)
        (7, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks BETWEEN 3 AND 50
              AND total_drivers > 0
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 18
        """),
        # Active FOR_HIRE, owner-op
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks BETWEEN 1 AND 2
              AND total_drivers BETWEEN 1 AND 2
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # Revoked / lapsed (INACTIVE)
        (6, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'INACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 14
        """),
        # Private, no MC
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND (mc_number IS NULL OR mc_number IN ('MC',''))
              AND total_trucks > 0
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # Large fleet (>50 trucks) — likely to have validation_conflicts
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks > 50
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # Fleet asymmetry (triggers validation_conflict)
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE (
                (total_trucks > 0 AND (total_drivers IS NULL OR total_drivers = 0))
                OR (total_drivers > 0 AND (total_trucks IS NULL OR total_trucks = 0))
            )
            AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # Carriers with no SMS scores row (null SMS data)
        (2, f"""
            SELECT c.dot_number FROM carriers c
            LEFT JOIN sms_scores s ON s.dot_number = c.dot_number
            WHERE s.dot_number IS NULL
              AND c.status = 'ACTIVE'
              AND c.mc_number IS NOT NULL AND c.mc_number NOT IN ('MC','')
              AND c.dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 6
        """),
    ]

    seen = set(EXCLUDE)
    selected = []
    cur = conn.cursor()
    for target, sql in strata:
        cur.execute(sql)
        rows = [r[0] for r in cur.fetchall() if r[0] not in seen]
        chosen = rows[:target]
        selected.extend(chosen)
        seen.update(chosen)
    cur.close()

    # top up to 27 if any stratum ran short
    if len(selected) < 27:
        short = 27 - len(selected)
        cur = conn.cursor()
        ex2 = "(" + ",".join(f"'{d}'" for d in seen) + ")"
        cur.execute(f"""
            SELECT dot_number FROM carriers
            WHERE dot_number NOT IN {ex2}
            ORDER BY RANDOM() LIMIT {short * 3}
        """)
        for (dot,) in cur.fetchall():
            if dot not in seen and len(selected) < 27:
                selected.append(dot)
                seen.add(dot)
        cur.close()

    return selected[:27]


# ── Report formatting (same template as _generate_audit_batch.py) ─────────────

def fleet_bucket(pu: int) -> str:
    if pu == 0:   return "0 (unverified)"
    if pu <= 2:   return "1-2 (owner-op)"
    if pu <= 10:  return "3-10 (small)"
    if pu <= 50:  return "11-50 (medium)"
    if pu <= 200: return "51-200 (large)"
    return "200+ (very large)"


def _display_sentinel(val: str) -> str:
    if val == "NOT_FOUND":
        return "No record found in imported dataset — verify against SAFER before relying on this field"
    return val


def _display_authority_status(val: str) -> str:
    if val == "NOT_FOUND":
        return ("No active authority record found in imported dataset. "
                "Possible causes: carrier never held authority, records not yet imported, "
                "or docket number mismatch. Verify against SAFER/L&I.")
    return val


def format_report(facts, results, conflicts, dot, ts, tag="") -> str:
    lines = []
    def p(s=""): lines.append(s)

    p("=" * W)
    p(f"CARRIER INTELLIGENCE REPORT — FMCSA DATA")
    p(f"Generated : {ts}  {tag}")
    p(f"DOT       : {dot}")
    p("=" * W)
    p()

    p("── CARRIER IDENTITY " + "─" * (W - 20))
    p(f"  Legal Name    : {facts.legal_name}")
    p(f"  DOT Number    : {facts.dot_number}")
    p(f"  MC Number     : {facts.mc_number or '(none)'}")
    p(f"  Carrier Type  : {facts.carrier_type}")
    p(f"  USDOT Status  : {_display_sentinel(facts.usdot_status)}")
    p()

    p("── CURRENT AUTHORITY " + "─" * (W - 21))
    p(f"  Required      : {facts.authority_required}")
    p(f"  Status        : {_display_authority_status(facts.authority_status)}")
    if facts.authority_status == CONFIRMED_REVOKED:
        p(f"  Revoc Date    : {facts.authority_revocation_date or '—'}")
    p(f"  First Auth    : {facts.first_authority_date or '—'}")
    p(f"  Active Period : {facts.active_authority_period or '—'}")
    p()

    auth_events = [r for r in facts._authority_records if r.get("status")]
    alert_revocs = [a for a in facts._alerts
                    if "INVOLUNTARY_REVOCATION" in (a.get("event_type") or "").upper()]
    if auth_events or alert_revocs:
        p("── HISTORICAL AUTHORITY EVENTS " + "─" * (W - 30))
        for rec in auth_events:
            eff  = str(rec.get("effective_date") or "—")[:10]
            revd = str(rec.get("revocation_date") or "")[:10]
            stat = rec.get("status") or "—"
            rsn  = rec.get("reason") or ""
            line = f"  {eff}  {stat}"
            if revd:
                line += f"  (resolved {revd})"
            if rsn:
                line += f"  [{rsn}]"
            p(line)
        for alert in alert_revocs:
            edate = str(alert.get("event_date") or "—")[:10]
            desc  = alert.get("description") or "INVOLUNTARY REVOCATION"
            p(f"  {edate}  ALERT: {desc}")
        p()

    p("── INSURANCE " + "─" * (W - 13))
    p(f"  Required      : {facts.insurance_required}")
    p(f"  Status        : {_display_sentinel(facts.insurance_status)}")
    p(f"  Cancellation  : {facts.insurance_cancellation}")
    p(f"  Replacement   : {facts.insurance_replacement}")
    p()

    p("── FLEET & OPERATIONS " + "─" * (W - 22))
    p(f"  Power Units   : {facts.fleet_power_units}  ({fleet_bucket(facts.fleet_power_units)})")
    p(f"  Drivers       : {facts.fleet_drivers}")
    p(f"  Inspections   : {facts.inspection_count}  (historical total, all available records; deduped)")
    p(f"  Crashes       : {facts.crash_count}  (historical total, all available records)")
    p(f"  Violations    : {facts.violation_count}  (historical total, all available records)")
    if facts.boc3_on_file == "YES":
        p(f"  BOC-3 on File : YES")
    else:
        p(f"  BOC-3 on File : BOC-3 status not verified from imported data — manual L&I/MOTUS verification required")
    p(f"  SMS Data      : {'Present' if facts.sms_percentile_present else 'Not present in imported dataset'}")
    p()

    p("── DATA CONFIDENCE " + "─" * (W - 19))
    for k, v in facts.data_confidence.items():
        p(f"  {k:<14}: {v}")
    p()

    n_pass = sum(1 for r in results if r.passed)
    n_fail = sum(1 for r in results if not r.passed)
    p("── VALIDATION RULES " + "─" * (W - 20))
    p(f"  {n_pass}/{len(results)} rules passed | {n_fail} failed")
    p()
    for r in results:
        icon = "PASS" if r.passed else "FAIL"
        p(f"  [{icon}] Rule {r.rule_id:02d} {r.rule_name}")
        p(f"         {r.message}")
    p()

    p("── VALIDATION CONFLICTS " + "─" * (W - 24))
    if not conflicts:
        p("  None detected.")
    else:
        p(f"  {len(conflicts)} conflict(s) found — manual SAFER verification recommended.")
        p()
        for i, c in enumerate(conflicts, 1):
            p(f"  [{i}] Rule : {c['rule']}")
            p(f"      Fields: {', '.join(c['fields'])}")
            wrapped = textwrap.fill(c["detail"], width=W - 12,
                                    initial_indent="      Detail: ",
                                    subsequent_indent="              ")
            p(wrapped)
    p()

    p("=" * W)
    p("Data source: FMCSA MCMIS via Socrata open data (data.transportation.gov)")
    p("Pipeline  : carrier_facts.py + validation_rules.py (Carrier Check USA)")
    p("=" * W)

    return "\n".join(lines)


# ── Verification checks ───────────────────────────────────────────────────────

def verify_bug_fixes(dot: str, facts, results) -> dict:
    """Return verification result dict for the 3 mandated DOTs."""
    checks = {}

    if dot in ("2051387", "888283"):
        # Bug 1: must be PRIVATE, no FOR_HIRE_INTERSTATE
        checks["carrier_type_is_PRIVATE"] = facts.carrier_type == "PRIVATE"
        checks["authority_required_NO"]    = facts.authority_required == "NO"
        checks["insurance_required_NO"]    = facts.insurance_required == "NO"
        # Must not display a fabricated MC number as active (mc_number may exist in DB
        # as an import artifact, but carrier_type must be PRIVATE regardless)
        checks["no_FOR_HIRE_classification"] = facts.carrier_type != "FOR_HIRE_INTERSTATE"

    if dot == "1074419":
        # Bug 2: CONFIRMED_ACTIVE with revocation_date cleared to None
        checks["authority_status_CONFIRMED_ACTIVE"] = facts.authority_status == "CONFIRMED_ACTIVE"
        checks["revocation_date_is_None"]           = facts.authority_revocation_date is None
        # Rule 01 must PASS (no active+revoked contradiction)
        rule01 = next((r for r in results if r.rule_name == "authority_not_both_active_and_revoked"), None)
        checks["rule_01_passes"]                    = rule01 is not None and rule01.passed

    return checks


# ── Main ──────────────────────────────────────────────────────────────────────

def build_with_retry(conn, dot, max_tries=3):
    for attempt in range(max_tries):
        try:
            return build_carrier_facts(conn, dot)
        except Exception as e:
            if attempt < max_tries - 1:
                try: conn.close()
                except: pass
                conn = psycopg2.connect(DB_URL, connect_timeout=30)
                conn.autocommit = True
            else:
                raise
    return None


def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = True

    print("Sampling 27 diversity DOTs...")
    random_27 = sample_27(conn)
    all_dots = MANDATED + random_27
    print(f"  Total DOTs to process: {len(all_dots)}  "
          f"(3 mandated + {len(random_27)} random)")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_rows = []
    written = []
    failed  = []
    verification_results = {}

    for i, dot in enumerate(all_dots, 1):
        is_mandated = dot in MANDATED
        tag = "[VERIFICATION]" if is_mandated else ""
        print(f"  [{i:02d}/30] DOT {dot} {tag}", end=" ... ", flush=True)

        # Reconnect every 10 carriers
        if i % 10 == 1 and i > 1:
            try: conn.close()
            except: pass
            conn = psycopg2.connect(DB_URL, connect_timeout=30)
            conn.autocommit = True

        try:
            facts = build_with_retry(conn, dot)
            if facts.legal_name == NOT_FOUND:
                print("NOT IN DB — skipped")
                failed.append(dot)
                continue

            results, conflicts = run_validation_with_conflicts(facts)
            n_fail = sum(1 for r in results if not r.passed)

            if is_mandated:
                verification_results[dot] = {
                    "legal_name": facts.legal_name,
                    "checks": verify_bug_fixes(dot, facts, results),
                    "carrier_type": facts.carrier_type,
                    "authority_status": facts.authority_status,
                    "authority_revocation_date": facts.authority_revocation_date,
                    "mc_number": facts.mc_number,
                }

            report_text = format_report(facts, results, conflicts, dot, ts, tag)
            out_path = OUTPUT_DIR / f"DOT_{dot}.txt"
            out_path.write_text(report_text, encoding="utf-8")

            manifest_rows.append({
                "dot_number":            dot,
                "legal_name":            facts.legal_name,
                "carrier_type":          facts.carrier_type,
                "usdot_status":          facts.usdot_status,
                "authority_status":      facts.authority_status,
                "fleet_power_units":     facts.fleet_power_units,
                "fleet_drivers":         facts.fleet_drivers,
                "fleet_bucket":          fleet_bucket(facts.fleet_power_units),
                "inspection_count":      facts.inspection_count,
                "sms_present":           facts.sms_percentile_present,
                "validation_failures":   n_fail,
                "has_conflicts":         "YES" if conflicts else "NO",
                "conflict_rules":        "; ".join(c["rule"] for c in conflicts),
                "is_verification_dot":   "YES" if is_mandated else "NO",
            })
            written.append(dot)
            status = (f"OK  ({n_fail} fail{'s' if n_fail != 1 else ''}, "
                      f"{'CONFLICT' if conflicts else 'clean'})")
            print(status)

        except Exception as exc:
            print(f"ERROR — {exc}")
            failed.append(dot)

    conn.close()

    # ── Manifest ──────────────────────────────────────────────────────────────
    manifest_path = OUTPUT_DIR / "manifest.csv"
    if manifest_rows:
        fieldnames = list(manifest_rows[0].keys())
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(manifest_rows)
        print(f"\nManifest written: {manifest_path.name}  ({len(manifest_rows)} rows)")

    # ── Verification report ───────────────────────────────────────────────────
    print(f"\n{'=' * W}")
    print("VERIFICATION RESULTS — 3 BUG-FIX DOTs")
    print("=" * W)
    all_checks_passed = True
    for dot, vr in verification_results.items():
        print(f"\nDOT {dot}  {vr['legal_name']}")
        print(f"  carrier_type          : {vr['carrier_type']}")
        print(f"  authority_status      : {vr['authority_status']}")
        print(f"  authority_revoc_date  : {vr['authority_revocation_date']}")
        print(f"  mc_number in DB       : {vr['mc_number']}")
        for check, passed in vr["checks"].items():
            icon = "PASS" if passed else "FAIL"
            print(f"  [{icon}] {check}")
            if not passed:
                all_checks_passed = False

    print()
    if all_checks_passed and verification_results:
        print("ALL VERIFICATION CHECKS PASSED — Bug 1 and Bug 2 fixes confirmed.")
    else:
        print("*** ONE OR MORE VERIFICATION CHECKS FAILED — review above ***")

    # ── Summary ───────────────────────────────────────────────────────────────
    with_conflicts = [r for r in manifest_rows if r["has_conflicts"] == "YES"]
    print(f"\n{'=' * W}")
    print("BATCH SUMMARY")
    print(f"  Reports written : {len(written)}")
    print(f"  Failed / skipped: {len(failed)}")
    print(f"  With conflicts  : {len(with_conflicts)}")
    print(f"  Output folder   : {OUTPUT_DIR}")
    print("=" * W)

    print("\nAll DOTs processed:")
    for r in manifest_rows:
        flag  = " [CONFLICT]"      if r["has_conflicts"] == "YES" else ""
        vtag  = " [VERIFICATION]"  if r["is_verification_dot"] == "YES" else ""
        print(f"  DOT {r['dot_number']:<10} {r['authority_status']:<25} "
              f"fleet={r['fleet_power_units']:<5} {r['legal_name'][:35]}{flag}{vtag}")

    if failed:
        print(f"\nFailed / skipped: {', '.join(failed)}")


if __name__ == "__main__":
    main()
