"""Regenerate 7 reports for Round 3 DOTs (intrastate dimension + fleet fix)."""
import os, sys, textwrap
import psycopg2
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from carrier_facts import build_carrier_facts, NOT_FOUND, CONFIRMED_REVOKED
from validation_rules import run_validation_with_conflicts

DB_URL = os.getenv("SUPABASE_DB_URL", "")
if not DB_URL:
    print("ERROR: SUPABASE_DB_URL not set"); sys.exit(1)

OUTPUT_DIR = Path(__file__).parent / "02 - Carrier Report 1st July 2026"
W = 80

TARGETS = ["31047", "810652", "973209", "4275752", "3128165", "1864365", "2833702"]


def fleet_bucket(pu):
    if pu == 0:   return "0 (unverified)"
    if pu <= 2:   return "1-2 (owner-op)"
    if pu <= 10:  return "3-10 (small)"
    if pu <= 50:  return "11-50 (medium)"
    if pu <= 200: return "51-200 (large)"
    return "200+ (very large)"


def _display_sentinel(val):
    if val == "NOT_FOUND":
        return ("No record found in imported dataset — verify against SAFER "
                "before relying on this field")
    return val


def _display_authority_status(val):
    if val == "NOT_FOUND":
        return ("No active authority record found in imported dataset. "
                "Possible causes: carrier never held authority, records not yet "
                "imported, or docket number mismatch. Verify against SAFER/L&I.")
    return val


def format_report(facts, results, conflicts, dot, ts, tag=""):
    lines = []
    def p(s=""): lines.append(s)

    p("=" * W)
    p("CARRIER INTELLIGENCE REPORT — FMCSA DATA")
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
    alert_revocs = [
        a for a in facts._alerts
        if "INVOLUNTARY_REVOCATION" in (a.get("event_type") or "").upper()
    ]
    if auth_events or alert_revocs:
        p("── HISTORICAL AUTHORITY EVENTS " + "─" * (W - 30))
        for rec in auth_events:
            eff  = str(rec.get("effective_date") or "—")[:10]
            revd = str(rec.get("revocation_date") or "")[:10]
            stat = rec.get("status") or "—"
            rsn  = rec.get("reason") or ""
            line = f"  {eff}  {stat}"
            if revd: line += f"  (resolved {revd})"
            if rsn:  line += f"  [{rsn}]"
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
    if facts.fleet_non_cmv_units > 0:
        p(f"  Non-CMV Units : {facts.fleet_non_cmv_units}  (cars, light vehicles — not counted as CMV power units)")
    if facts.has_passenger_cargo:
        p("  [NOTICE] MCS-150 lists Passengers as cargo type. Verify passenger carrier"
          " registration and insurance separately before relying on any 'not required'"
          " statements in this report.")
    p(f"  Drivers       : {facts.fleet_drivers}")
    p(f"  Inspections   : {facts.inspection_count}  (historical total, all available records; deduped)")
    p(f"  Crashes       : {facts.crash_count}  (historical total, all available records)")
    p(f"  Violations    : {facts.violation_count}  (historical total, all available records)")
    if facts.boc3_on_file == "YES":
        p("  BOC-3 on File : YES")
    else:
        p("  BOC-3 on File : BOC-3 status not verified from imported data "
          "— manual L&I/MOTUS verification required")
    p(f"  SMS Data      : "
      f"{'Present' if facts.sms_percentile_present else 'Not present in imported dataset'}")
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
            wrapped = textwrap.fill(
                c["detail"], width=W - 12,
                initial_indent="      Detail: ",
                subsequent_indent="              ")
            p(wrapped)
    p()

    p("=" * W)
    p("Data source: FMCSA MCMIS via Socrata open data (data.transportation.gov)")
    p("Pipeline  : carrier_facts.py + validation_rules.py (Carrier Check USA)")
    p("=" * W)
    return "\n".join(lines)


def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = True
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nRegenerating {len(TARGETS)} Round 3 reports → {OUTPUT_DIR.name}/\n")
    for dot in TARGETS:
        print(f"  DOT {dot} ...", end=" ", flush=True)
        facts = build_carrier_facts(conn, dot)
        if facts.legal_name == NOT_FOUND:
            print("NOT IN DB — skipped")
            continue
        results, conflicts = run_validation_with_conflicts(facts)
        n_fail = sum(1 for r in results if not r.passed)
        report_text = format_report(facts, results, conflicts, dot, ts, "[ROUND-3-FIX]")
        out_path = OUTPUT_DIR / f"DOT_{dot}.txt"
        out_path.write_text(report_text, encoding="utf-8")
        print(f"OK  carrier_type={facts.carrier_type}  "
              f"({n_fail} fail, {'CONFLICT' if conflicts else 'clean'})"
              + (f"  non_cmv={facts.fleet_non_cmv_units}" if facts.fleet_non_cmv_units > 0 else "")
              + ("  [PAX]" if facts.has_passenger_cargo else ""))

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
