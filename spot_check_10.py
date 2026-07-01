"""
spot_check_10.py
================
Randomly picks 10 DOT numbers from the DB and cross-checks every key field
against TWO authoritative sources:

  Source 1 — FMCSA SAFER web page (official public record)
    https://safer.fmcsa.dot.gov/
    Checks: operating status, legal name, MC number, fleet size,
            safety rating, drivers, power units

  Source 2 — FMCSA Socrata datasets (data.transportation.gov)
    Checks: crash count, inspection count, insurance count,
            carrier name, status

Prints a pass/fail report and saves it to spot_check_report.txt

Usage:
  python spot_check_10.py               # 10 random carriers
  python spot_check_10.py --n 30        # N random carriers
  python spot_check_10.py --dot 204814 2259497   # specific DOTs
"""

import os, sys, re, time, requests, psycopg2
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URL    = os.getenv("SUPABASE_DB_URL", "")
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
HEADERS   = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
BASE      = "https://data.transportation.gov/resource"

# Socrata dataset IDs (verified from reimport scripts — source of truth: fmcsa_import.py)
DS_CARRIER  = "az4n-8mr2"   # Carrier census: status, fleet, MC# — field: dot_number
DS_CRASH    = "aayw-vxb3"   # Crashes — field: dot_number
DS_INSP     = "fx4q-ay7w"   # Inspections — field: dot_number
DS_INS_HIST = "6sqe-dvqs"   # Insurance history — field: dot_number (fallback: docket)
DS_AUTH     = "9mw4-x3tu"   # Authority history — field: dot_number
DS_SMS      = "m3ry-qcip"   # SMS scores — field: dot_number


# ── Helpers ───────────────────────────────────────────────────────────────────

def norm(s):
    """Normalise a string for loose comparison."""
    if not s:
        return ""
    return " ".join(str(s).upper().split())

def match(a, b, loose=True):
    """True if values match. loose=True allows substring match."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return None          # can't compare — one side missing
    if loose:
        return a in b or b in a or a == b
    return a == b

def icon(result):
    if result is True:  return "✓"
    if result is False: return "✗"
    return "?"


# ── SAFER web scraper ─────────────────────────────────────────────────────────

SAFER_URL = "https://safer.fmcsa.dot.gov/query.asp"

def safer_field(html, label):
    """Extract a table cell value from SAFER HTML by its label."""
    pattern = rf'{re.escape(label)}</th>\s*<td[^>]*>(.*?)</td>'
    m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    raw = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    return raw if raw else None

def fetch_safer(dot):
    """Fetch carrier snapshot from FMCSA SAFER web page."""
    try:
        r = requests.get(
            SAFER_URL,
            params={
                "searchtype": "ANY",
                "query_type": "queryCarrierSnapshot",
                "query_param": "USDOT",
                "query_string": dot,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://safer.fmcsa.dot.gov/",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        html = r.text
        if "No records found" in html or "no carrier" in html.lower():
            return None, "Not found in SAFER"

        data = {
            "legal_name":      safer_field(html, "Legal Name:"),
            "dba_name":        safer_field(html, "DBA Name:"),
            "status":          safer_field(html, "Operating Status:"),
            "mc_number":       safer_field(html, "MC/MX/FF Number(s):"),
            "entity_type":     safer_field(html, "Entity Type:"),
            "safety_rating":   safer_field(html, "Safety Rating:"),
            "rating_date":     safer_field(html, "Rating Date:"),
            "review_date":     safer_field(html, "Review Date:"),
            "power_units":     safer_field(html, "Power Units:"),
            "drivers":         safer_field(html, "Drivers:"),
            "address":         safer_field(html, "Physical Address:"),
            "phone":           safer_field(html, "Phone:"),
            "cargo_type":      safer_field(html, "Carrier Operation:"),
        }
        return data, None
    except Exception as e:
        return None, str(e)


# ── Socrata fetchers ──────────────────────────────────────────────────────────

def socrata_carrier(dot):
    try:
        r = requests.get(
            f"{BASE}/{DS_CARRIER}.json",
            params={
                "$where": f"dot_number='{dot}'",
                "$select": "dot_number,legal_name,status_code,total_drivers,power_units,"
                           "docket1prefix,docket1,docket2prefix,docket2",
                "$limit": 1,
            },
            headers=HEADERS, timeout=20,
        )
        rows = r.json()
        if not rows:
            return None
        rec = rows[0]
        mc = None
        for i in ("1", "2"):
            p = (rec.get(f"docket{i}prefix") or "").strip()
            n = (rec.get(f"docket{i}") or "").strip()
            if p and n:
                mc = f"{p}{n.zfill(6)}"
                break
        return {
            "legal_name":    rec.get("legal_name", "").strip(),
            "status":        rec.get("status_code", "").strip(),
            "total_drivers": rec.get("total_drivers"),
            "power_units":   rec.get("power_units"),
            "mc_number":     mc,
        }
    except Exception:
        return None

def socrata_count(dataset, dot, field="dot_number"):
    try:
        r = requests.get(
            f"{BASE}/{dataset}.json",
            params={"$select": "count(:id)", "$where": f"{field}='{dot}'"},
            headers=HEADERS, timeout=20,
        )
        return int(r.json()[0]["count_id"])
    except Exception:
        return None

def socrata_crash_count(dot):
    return socrata_count(DS_CRASH, dot)

def socrata_insurance_count(dot, mc_number=None):
    """Count ALL insurance records (history + active) to match our combined insurance table.
    - InsHist (6sqe-dvqs): docket_number = MC771154, or dot_number
    - ActPendInsur (ypjt-5ydn): prefix_docket_number = MC771154
    Our DB combines both datasets into one insurance table."""
    hist = 0
    active = 0

    # Insurance history (6sqe-dvqs)
    if mc_number:
        h = socrata_count(DS_INS_HIST, mc_number, field="docket_number")
        hist = h if h is not None else 0
    if hist == 0:
        h = socrata_count(DS_INS_HIST, dot)
        hist = h if h is not None else 0

    # Active/pending insurance (ypjt-5ydn) — uses prefix_docket_number
    if mc_number:
        a = socrata_count("ypjt-5ydn", mc_number, field="prefix_docket_number")
        active = a if a is not None else 0

    total = hist + active
    return total if (hist is not None or active is not None) else None

def socrata_auth_count(dot, mc_number=None):
    """Count authority history records.
    Socrata 9mw4-x3tu is docket-linked (docket_number=MC771154), not DOT-linked.
    Always use docket_number if available; fall back to dot_number."""
    if mc_number:
        count = socrata_count(DS_AUTH, mc_number, field="docket_number")
        if count is not None:
            return count
    return socrata_count(DS_AUTH, dot)

def socrata_ins_act_count(dot):
    """Count active insurance policies from ActPendInsur via docket lookup."""
    try:
        # Get docket numbers for this carrier from Socrata carrier dataset
        r = requests.get(
            f"{BASE}/{DS_CARRIER}.json",
            params={
                "$where": f"dot_number='{dot}'",
                "$select": "docket1prefix,docket1",
                "$limit": 1,
            },
            headers=HEADERS, timeout=15,
        )
        rows = r.json()
        if not rows:
            return None
        rec = rows[0]
        p = (rec.get("docket1prefix") or "").strip()
        n = (rec.get("docket1") or "").strip()
        if not p or not n:
            return None
        docket = f"{p}{n}"
        r2 = requests.get(
            f"{BASE}/{DS_INS_ACT}.json",
            params={"$select": "count(:id)", "prefix_docket_number": docket},
            headers=HEADERS, timeout=15,
        )
        return int(r2.json()[0]["count_id"])
    except Exception:
        return None


# ── DB fetch ──────────────────────────────────────────────────────────────────

def fetch_db(conn, dot):
    cur = conn.cursor()
    cur.execute("""
        SELECT legal_name, mc_number, status, total_drivers, total_trucks,
               cargo_type, safety_rating, safety_rating_date
        FROM carriers WHERE dot_number = %s
    """, (dot,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ["legal_name","mc_number","status","total_drivers","total_trucks",
            "cargo_type","safety_rating","safety_rating_date"]
    carrier = dict(zip(keys, row))

    cur.execute("SELECT COUNT(*) FROM crashes WHERE dot_number = %s", (dot,))
    carrier["crash_count"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM inspections WHERE dot_number = %s", (dot,))
    carrier["insp_count"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM violations WHERE dot_number = %s", (dot,))
    carrier["viol_count"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM insurance WHERE dot_number = %s", (dot,))
    carrier["ins_count"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM authority_history WHERE dot_number = %s", (dot,))
    carrier["auth_count"] = cur.fetchone()[0]

    cur.close()
    return carrier


# ── Report ────────────────────────────────────────────────────────────────────

def check_carrier(dot, db, safer, socrata):
    lines = []
    passed = failed = unknown = 0

    def row(label, db_val, src_val, src_name, loose=True):
        nonlocal passed, failed, unknown
        result = match(db_val, src_val, loose)
        ic = icon(result)
        if result is True:  passed  += 1
        elif result is False: failed += 1
        else: unknown += 1
        db_str  = str(db_val)[:40]  if db_val  else "—"
        src_str = str(src_val)[:40] if src_val else "—"
        lines.append(f"  {ic}  {label:<28} DB={db_str:<42} {src_name}={src_str}")

    lines.append(f"\nDOT {dot} — {db['legal_name'] or '(unknown)'}")
    lines.append("  " + "─" * 100)

    # ── vs SAFER ──────────────────────────────────────────────────────────────
    safer_url = (f"https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY"
                 f"&query_type=queryCarrierSnapshot&query_param=USDOT&query_string={dot}")
    if safer:
        lines.append("  [vs SAFER]")
        row("Legal name",    db["legal_name"],    safer.get("legal_name"),    "SAFER")
        row("Status",        db["status"],         safer.get("status"),        "SAFER")
        row("MC number",     db["mc_number"],      safer.get("mc_number"),     "SAFER")
        row("Drivers",       db["total_drivers"],  safer.get("drivers"),       "SAFER", loose=False)
        row("Power units",   db["total_trucks"],   safer.get("power_units"),   "SAFER", loose=False)
        row("Safety rating", db["safety_rating"],  safer.get("safety_rating"), "SAFER")
    else:
        # Gap 3 mitigation: SAFER is Cloudflare-blocked for automated requests.
        # Socrata (data.transportation.gov) is the same MCMIS source — use it instead.
        lines.append(f"  [SAFER blocked] Manual URL: {safer_url}")
        lines.append("  [vs Socrata — covers same MCMIS data as SAFER]")

    # ── vs Socrata ────────────────────────────────────────────────────────────
    if socrata:
        lines.append("  [vs Socrata]")
        row("Legal name",     db["legal_name"],     socrata.get("legal_name"),   "Socrata")
        row("Status",         db["status"],          socrata.get("status"),       "Socrata")
        row("MC number",      db["mc_number"],       socrata.get("mc_number"),    "Socrata")
        row("Drivers",        db["total_drivers"],   socrata.get("total_drivers"),"Socrata", loose=False)
        row("Power units",    db["total_trucks"],    socrata.get("power_units"),  "Socrata", loose=False)
    else:
        lines.append("  [Socrata] unavailable — skipped")

    # ── Counts (Socrata vs DB) ────────────────────────────────────────────────
    lines.append("  [Record counts: DB vs Socrata live]")
    for label, db_val, src_val in [
        ("Crashes",        db["crash_count"],  socrata_crash_count(dot)),
        ("Inspections",    db["insp_count"],   socrata_count(DS_INSP,  dot)),
        ("Insurance hist", db["ins_count"],    socrata_insurance_count(dot, db.get("mc_number"))),
        ("Auth history",   db["auth_count"],   socrata_auth_count(dot, db.get("mc_number"))),
    ]:
        result = (db_val == src_val) if src_val is not None else None
        ic = icon(result)
        if result is True:  passed  += 1
        elif result is False: failed += 1
        else: unknown += 1
        src_str = str(src_val) if src_val is not None else "?"
        lines.append(f"  {ic}  {label:<28} DB={db_val:<42} Socrata={src_str}")

    lines.append(f"  {'─'*100}")
    lines.append(f"  RESULT: {passed} passed  {failed} failed  {unknown} unknown")
    return lines, passed, failed, unknown


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Parse args
    specific_dots = []
    n_random = 10
    if "--dot" in sys.argv:
        idx = sys.argv.index("--dot")
        specific_dots = sys.argv[idx+1:]
    if "--n" in sys.argv:
        idx = sys.argv.index("--n")
        try:
            n_random = int(sys.argv[idx+1])
        except (IndexError, ValueError):
            pass

    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '120s'")

    if specific_dots:
        dots = specific_dots
    else:
        cur.execute(f"""
            SELECT dot_number FROM carriers
            WHERE legal_name IS NOT NULL
              AND status = 'ACTIVE'
              AND total_trucks > 0
            ORDER BY RANDOM()
            LIMIT {n_random}
        """)
        dots = [r[0] for r in cur.fetchall()]
    cur.close()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_lines = [
        "=" * 110,
        f"SPOT CHECK REPORT — {timestamp}",
        f"Checking {len(dots)} carriers against SAFER + Socrata",
        "=" * 110,
    ]

    total_passed = total_failed = total_unknown = 0

    for i, dot in enumerate(dots, 1):
        print(f"[{i}/{len(dots)}] DOT {dot}...", end=" ", flush=True)

        db = fetch_db(conn, dot)
        if not db:
            print("not in DB — skipped")
            continue

        safer, err = fetch_safer(dot)
        if err:
            print(f"SAFER: {err}", end=" ")
        socrata = socrata_carrier(dot)

        lines, p, f, u = check_carrier(dot, db, safer, socrata)
        total_passed  += p
        total_failed  += f
        total_unknown += u

        report_lines.extend(lines)
        status = "✓" if f == 0 else f"✗ {f} mismatch(es)"
        print(status)

        time.sleep(0.5)   # be polite to SAFER

    conn.close()

    total = total_passed + total_failed
    pct = total_passed / total * 100 if total else 0
    report_lines += [
        "",
        "=" * 110,
        f"OVERALL: {total_passed}/{total} fields matched ({pct:.1f}%) | {total_failed} mismatches | {total_unknown} unknown",
        "=" * 110,
        "",
        "✓ = DB matches source   ✗ = mismatch   ? = one side missing/unavailable",
        "Unlinked record counts (inspections, crashes) may differ slightly if",
        "FMCSA has updated since our last import.",
    ]

    report = "\n".join(report_lines)
    print("\n" + report)

    out_file = "spot_check_report.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nSaved to {out_file}")


if __name__ == "__main__":
    main()
