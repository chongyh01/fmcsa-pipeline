"""
_generate_batch03.py
====================
Batch 03 — 30 random carrier reports for external audit.

Excludes:
  - 12 gold_carriers.json DOTs
  - 30 DOTs from batch 01 (Carrier Report 1st July 2026)
  - 30 DOTs from batch 02 (02 - Carrier Report 1st July 2026)
  - 3 mandated DOTs from batch 02

Output:
  CODES/03 - Carrier Report 1st July 2026/DOT_<number>.txt
  CODES/03 - Carrier Report 1st July 2026/manifest.csv

Usage:
    python _generate_batch03.py
"""

import os, sys, csv, textwrap
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

OUTPUT_DIR = Path(__file__).parent / "03 - Carrier Report 1st July 2026"
OUTPUT_DIR.mkdir(exist_ok=True)

W = 80

# ── Exclusion sets ─────────────────────────────────────────────────────────────

GOLD_DOTS = {
    "204814", "3431540", "3612954", "3841767",
    "623336", "3012101", "1612145", "2308088",
    # Round 2 gold additions
    "2051387", "888283", "830598", "833248",
    "2383948", "3036633", "2675124",
}

BATCH01_DOTS = {
    "2898056","675795","539473","488677","1302916","1372987","3411477","3384102",
    "3105097","3933118","4021252","4080399","1528750","1732647","3496197","4076555",
    "611058","2933699","2069221","1810135","4512553","2041810","1788189","713937",
    "919057","987933","1181967","2719904","2615378","4046894",
}

BATCH02_DOTS = {
    "1074419","1017339","1083818","1258131","1346690","142781","1605056","194077",
    "1982156","2175746","2292725","2322078","2456715","2647384","2846625","2959196",
    "3049985","3061400","3150227","3672715","3917322","3990182","4307941","4434356",
    "648040",
}

EXCLUDE = GOLD_DOTS | BATCH01_DOTS | BATCH02_DOTS

N_TARGET = 30


# ── Sampling ──────────────────────────────────────────────────────────────────

def sample_30(conn) -> list[str]:
    ex = "(" + ",".join(f"'{d}'" for d in EXCLUDE) + ")"
    strata = [
        # Active FOR_HIRE, small-medium fleet
        (8, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks BETWEEN 3 AND 50
              AND total_drivers > 0
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 20
        """),
        # Active FOR_HIRE, owner-op
        (4, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks BETWEEN 1 AND 2
              AND total_drivers BETWEEN 1 AND 2
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 10
        """),
        # Revoked / lapsed (INACTIVE)
        (6, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'INACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 15
        """),
        # Private, no MC
        (4, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND (mc_number IS NULL OR mc_number IN ('MC',''))
              AND total_trucks > 0
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 10
        """),
        # Large fleet (> 50 trucks)
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE status = 'ACTIVE'
              AND mc_number IS NOT NULL AND mc_number NOT IN ('MC','')
              AND total_trucks > 50
              AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # Fleet asymmetry (drivers=0 or trucks=0)
        (3, f"""
            SELECT dot_number FROM carriers
            WHERE (
                (total_trucks > 0 AND (total_drivers IS NULL OR total_drivers = 0))
                OR (total_drivers > 0 AND (total_trucks IS NULL OR total_trucks = 0))
            )
            AND dot_number NOT IN {ex}
            ORDER BY RANDOM() LIMIT 8
        """),
        # No SMS row
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

    if len(selected) < N_TARGET:
        short = N_TARGET - len(selected)
        cur = conn.cursor()
        ex2 = "(" + ",".join(f"'{d}'" for d in seen) + ")"
        cur.execute(f"""
            SELECT dot_number FROM carriers
            WHERE dot_number NOT IN {ex2}
            ORDER BY RANDOM() LIMIT {short * 3}
        """)
        for (dot,) in cur.fetchall():
            if dot not in seen and len(selected) < N_TARGET:
                selected.append(dot)
                seen.add(dot)
        cur.close()

    return selected[:N_TARGET]


# ── Report formatting ─────────────────────────────────────────────────────────

def fleet_bucket(pu: int) -> str:
    if pu == 0:   return "0 (unverified)"
    if pu <= 2:   return "1-2 (owner-op)"
    if pu <= 10:  return "3-10 (small)"
    if pu <= 50:  return "11-50 (medium)"
    if pu <= 200: return "51-200 (large)"
    return "200+ (very large)"


def _display_sentinel(val: str) -> str:
    if val == "NOT_FOUND":
        return ("No record found in imported dataset — verify against SAFER "
                "before relying on this field")
    return val


def _display_authority_status(val: str) -> str:
    if val == "NOT_FOUND":
        return ("No active authority record found in imported dataset. "
                "Possible causes: carrier never held authority, records not yet "
                "imported, or docket number mismatch. Verify against SAFER/L&I.")
    return val


def format_report(facts, results, conflicts, dot, ts) -> str:
    lines = []
    def p(s=""): lines.append(s)

    p("=" * W)
    p("CARRIER INTELLIGENCE REPORT — FMCSA DATA")
    p(f"Generated : {ts}")
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = True

    print(f"Sampling {N_TARGET} DOTs for batch 03...")
    dots = sample_30(conn)
    print(f"  Got {len(dots)} DOTs\n")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_rows = []
    written = []
    failed  = []

    for i, dot in enumerate(dots, 1):
        print(f"  [{i:02d}/{N_TARGET}] DOT {dot}", end=" ... ", flush=True)

        if i % 10 == 1 and i > 1:
            try: conn.close()
            except: pass
            conn = psycopg2.connect(DB_URL, connect_timeout=30)
            conn.autocommit = True

        try:
            facts = build_carrier_facts(conn, dot)
            if facts.legal_name == NOT_FOUND:
                print("NOT IN DB — skipped")
                failed.append(dot)
                continue

            results, conflicts = run_validation_with_conflicts(facts)
            n_fail = sum(1 for r in results if not r.passed)

            report_text = format_report(facts, results, conflicts, dot, ts)
            out_path = OUTPUT_DIR / f"DOT_{dot}.txt"
            out_path.write_text(report_text, encoding="utf-8")

            manifest_rows.append({
                "dot_number":          dot,
                "legal_name":          facts.legal_name,
                "carrier_type":        facts.carrier_type,
                "usdot_status":        facts.usdot_status,
                "authority_status":    facts.authority_status,
                "fleet_power_units":   facts.fleet_power_units,
                "fleet_drivers":       facts.fleet_drivers,
                "fleet_bucket":        fleet_bucket(facts.fleet_power_units),
                "inspection_count":    facts.inspection_count,
                "sms_present":         facts.sms_percentile_present,
                "validation_failures": n_fail,
                "has_conflicts":       "YES" if conflicts else "NO",
                "conflict_rules":      "; ".join(c["rule"] for c in conflicts),
            })
            written.append(dot)
            status = (f"OK  ({n_fail} fail{'s' if n_fail != 1 else ''}, "
                      f"{'CONFLICT' if conflicts else 'clean'})")
            print(status)

        except Exception as exc:
            print(f"ERROR — {exc}")
            failed.append(dot)

    conn.close()

    manifest_path = OUTPUT_DIR / "manifest.csv"
    if manifest_rows:
        fieldnames = list(manifest_rows[0].keys())
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(manifest_rows)

    print(f"\n{'=' * W}")
    print("BATCH 03 SUMMARY")
    print(f"  Reports written : {len(written)}")
    print(f"  Failed / skipped: {len(failed)}")
    with_conflicts = [r for r in manifest_rows if r["has_conflicts"] == "YES"]
    print(f"  With conflicts  : {len(with_conflicts)}")
    print(f"  Output folder   : {OUTPUT_DIR}")
    print("=" * W)

    print("\nAll DOTs:")
    for r in manifest_rows:
        flag = " [CONFLICT]" if r["has_conflicts"] == "YES" else ""
        print(f"  DOT {r['dot_number']:<10} {r['carrier_type']:<22} "
              f"fleet={r['fleet_power_units']:<5} {r['legal_name'][:35]}{flag}")


if __name__ == "__main__":
    main()
