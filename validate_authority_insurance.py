"""
validate_authority_insurance.py
===============================
Samples carriers from each authority/insurance category and outputs a report
for manual SAFER verification. Run after imports are complete.

Checks:
- 20 carriers expected to have ACTIVE authority
- 20 carriers expected to have REVOKED authority
- 20 carriers expected to have REINSTATED authority
- 20 carriers expected to have CANCELLED insurance
- 20 carriers expected to have REPLACED insurance

Uses TEST_DATE (today) as the accident date for all derivations.
Output: printed report + saved to validate_authority_insurance_report.txt
"""

import os, sys, psycopg2
from datetime import date
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_URL    = os.environ["SUPABASE_DB_URL"]
TEST_DATE = str(date.today())  # 2026-06-22

conn = psycopg2.connect(DB_URL, connect_timeout=30)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SET statement_timeout = '120s'")

lines = []

def pr(s=""):
    print(s)
    lines.append(s)

def fmt(d):
    if not d:
        return "—"
    try:
        return str(d)[:10]
    except Exception:
        return str(d)

pr(f"AUTHORITY / INSURANCE STATUS VALIDATION REPORT")
pr(f"Test date: {TEST_DATE}")
pr(f"=" * 70)

# ─── 1. ACTIVE AUTHORITY CARRIERS ─────────────────────────────────────────────
pr("\n--- ACTIVE AUTHORITY (should show ACTIVE on today's date) ---")
pr("(Pick carriers that received GRANTED with no subsequent REVOKED)")
pr()
cur.execute("""
    SELECT c.dot_number, c.legal_name, c.mc_number,
           ah.authority_type, ah.effective_date, ah.revocation_date
    FROM carriers c
    JOIN authority_history ah ON ah.dot_number = c.dot_number
    WHERE ah.status ILIKE '%GRANT%'
      AND ah.revocation_date IS NULL
      AND ah.effective_date IS NOT NULL
    ORDER BY RANDOM()
    LIMIT 20
""")
rows = cur.fetchall()
pr(f"{'DOT':<12} {'MC#':<14} {'Auth Type':<30} {'Granted':<12} {'Revoked':<12} {'Name'}")
pr("-" * 110)
for dot, name, mc, atype, eff, rev in rows:
    pr(f"{dot:<12} {(mc or '—'):<14} {(atype or '—')[:29]:<30} {fmt(eff):<12} {fmt(rev):<12} {(name or '')[:40]}")

# ─── 2. REVOKED AUTHORITY CARRIERS ────────────────────────────────────────────
pr(f"\n\n--- REVOKED AUTHORITY (should show INACTIVE/REVOKED on today's date) ---")
pr("(Carriers with INVOLUNTARY REVOCATION and no later reinstatement)")
pr()
# Step 1: get sample DOTs with involuntary revocation
cur.execute("""
    SELECT dot_number, authority_type, effective_date, reason
    FROM authority_history
    WHERE status ILIKE '%INVOLUNTARY%'
      AND (reason IS NULL OR reason NOT ILIKE '%DISCONTINUED%')
      AND effective_date IS NOT NULL
    LIMIT 200
""")
inv_rows = cur.fetchall()
# Filter to those without a later reinstatement (in Python to avoid slow subquery)
reinst_dots = set()
cur.execute("SELECT DISTINCT dot_number FROM authority_history WHERE status ILIKE '%REINSTATED%'")
for (d,) in cur.fetchall():
    reinst_dots.add(d)
revoked_sample = [(d, at, eff, r) for (d, at, eff, r) in inv_rows if d not in reinst_dots][:20]
# Fetch carrier names
if revoked_sample:
    dots = [r[0] for r in revoked_sample]
    cur.execute(f"SELECT dot_number, legal_name, mc_number FROM carriers WHERE dot_number = ANY(%s)", (dots,))
    carrier_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pr(f"{'DOT':<12} {'MC#':<14} {'Auth Type':<25} {'Rev Date':<12} {'Name'}")
    pr("-" * 90)
    for dot, atype, eff, reason in revoked_sample:
        name, mc = carrier_map.get(dot, ("—", None))
        pr(f"{dot:<12} {(mc or '—'):<14} {(atype or '—')[:24]:<25} {fmt(eff):<12} {(name or '')[:35]}")
else:
    pr("  No revoked-only carriers found.")

# ─── 3. REINSTATED AUTHORITY CARRIERS ─────────────────────────────────────────
pr(f"\n\n--- REINSTATED AUTHORITY (should show ACTIVE after reinstatement) ---")
pr()
cur.execute("""
    SELECT DISTINCT ON (dot_number)
           dot_number, authority_type, effective_date, status
    FROM authority_history
    WHERE status ILIKE '%REINSTATED%'
      AND effective_date IS NOT NULL
    ORDER BY dot_number, effective_date DESC
    LIMIT 20
""")
reinst_rows = cur.fetchall()
if reinst_rows:
    dots = [r[0] for r in reinst_rows]
    cur.execute(f"SELECT dot_number, legal_name, mc_number FROM carriers WHERE dot_number = ANY(%s)", (dots,))
    carrier_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pr(f"{'DOT':<12} {'MC#':<14} {'Auth Type':<25} {'Reinst Date':<14} {'Status':<30} {'Name'}")
    pr("-" * 115)
    for dot, atype, eff, status in reinst_rows:
        name, mc = carrier_map.get(dot, ("—", None))
        pr(f"{dot:<12} {(mc or '—'):<14} {(atype or '—')[:24]:<25} {fmt(eff):<14} {(status or '')[:29]:<30} {(name or '')[:30]}")

# ─── 4. CANCELLED INSURANCE CARRIERS ──────────────────────────────────────────
pr(f"\n\n--- CANCELLED INSURANCE (most recent policy should be CANCELLED) ---")
pr()
cur.execute("""
    SELECT DISTINCT ON (dot_number)
           dot_number, insurer_name, effective_date, cancellation_date, status
    FROM insurance
    WHERE status ILIKE '%cancel%'
      AND cancellation_date IS NOT NULL
      AND effective_date IS NOT NULL
    ORDER BY dot_number, cancellation_date DESC
    LIMIT 20
""")
canc_rows = cur.fetchall()
if canc_rows:
    dots = [r[0] for r in canc_rows]
    cur.execute(f"SELECT dot_number, legal_name, mc_number FROM carriers WHERE dot_number = ANY(%s)", (dots,))
    carrier_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pr(f"{'DOT':<12} {'MC#':<14} {'Insurer':<30} {'Effective':<12} {'Cancelled':<12} {'Name'}")
    pr("-" * 110)
    for dot, insurer, eff, cancel, status in canc_rows:
        name, mc = carrier_map.get(dot, ("—", None))
        pr(f"{dot:<12} {(mc or '—'):<14} {(insurer or '—')[:29]:<30} {fmt(eff):<12} {fmt(cancel):<12} {(name or '')[:35]}")

# ─── 5. REPLACED INSURANCE CARRIERS ───────────────────────────────────────────
pr(f"\n\n--- REPLACED INSURANCE (policy was replaced — successor should be active) ---")
pr()
cur.execute("""
    SELECT DISTINCT ON (dot_number)
           dot_number, insurer_name, effective_date, cancellation_date, status
    FROM insurance
    WHERE status ILIKE '%replac%'
      AND cancellation_date IS NOT NULL
    ORDER BY dot_number, cancellation_date DESC
    LIMIT 20
""")
repl_rows = cur.fetchall()
if repl_rows:
    dots = [r[0] for r in repl_rows]
    cur.execute(f"SELECT dot_number, legal_name, mc_number FROM carriers WHERE dot_number = ANY(%s)", (dots,))
    carrier_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pr(f"{'DOT':<12} {'MC#':<14} {'Insurer':<30} {'Effective':<12} {'Replaced':<12} {'Name'}")
    pr("-" * 110)
    for dot, insurer, eff, cancel, status in repl_rows:
        name, mc = carrier_map.get(dot, ("—", None))
        pr(f"{dot:<12} {(mc or '—'):<14} {(insurer or '—')[:29]:<30} {fmt(eff):<12} {fmt(cancel):<12} {(name or '')[:35]}")

cur.close()
conn.close()

pr(f"\n{'=' * 70}")
pr("TO VALIDATE: Open FMCSA SAFER at https://safer.fmcsa.dot.gov/")
pr("For each DOT number above, look up the carrier and verify:")
pr("  Authority: does SAFER show the same active/revoked/reinstated status?")
pr("  Insurance: does SAFER show the same cancelled/replaced/active status?")
pr(f"{'=' * 70}")

# Save to file
out = "validate_authority_insurance_report.txt"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"\nReport saved to {out}")
