"""
validate_data_quality.py
========================
Read-only audit of all known data quality issues in the Carrier Check USA DB.
Outputs: validation_report.md (same directory as this script).

Run: python validate_data_quality.py
"""
import os, json, re, psycopg2
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("SUPABASE_DB_URL")

CODES_DIR   = os.path.dirname(os.path.abspath(__file__))
PORTAL_DIR  = os.path.join(CODES_DIR, "..", "carrier-portal")
CFR_JSON    = os.path.join(PORTAL_DIR, "app", "carrier", "[dot]", "cfr_descriptions.json")
REPORT_PATH = os.path.join(CODES_DIR, "validation_report.md")

def get_conn():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = 0")  # no per-query timeout — queries can be slow
    cur.close()
    return conn

def q(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    return rows

print("Connecting to database...", flush=True)
conn = get_conn()

# ── Total carriers (denominator for % calcs) ──────────────────────────
print("Getting total carrier count...", flush=True)
total_carriers = q(conn, "SELECT COUNT(*) FROM carriers")[0][0]
print(f"Total carriers: {total_carriers:,}", flush=True)

results = {}

# ══════════════════════════════════════════════════════════════════
# CHECK 1 — Fleet 0/0 (driver & truck counts both zero)
# ══════════════════════════════════════════════════════════════════
print("\nCheck 1: Fleet 0/0...", flush=True)

fleet_zero_total = q(conn, """
    SELECT COUNT(*)
    FROM carriers
    WHERE (total_drivers = 0 OR total_drivers IS NULL)
      AND (total_trucks  = 0 OR total_trucks  IS NULL)
""")[0][0]

# How many of those are active for-hire (most suspicious — should have fleet data)
fleet_zero_active_forhire = q(conn, """
    SELECT COUNT(*)
    FROM carriers
    WHERE (total_drivers = 0 OR total_drivers IS NULL)
      AND (total_trucks  = 0 OR total_trucks  IS NULL)
      AND status = 'ACTIVE'
      AND (cargo_type ILIKE '%%for hire%%' OR cargo_type ILIKE '%%authorized%%')
""")[0][0]

fleet_zero_examples = q(conn, """
    SELECT dot_number, legal_name, status, cargo_type
    FROM carriers
    WHERE (total_drivers = 0 OR total_drivers IS NULL)
      AND (total_trucks  = 0 OR total_trucks  IS NULL)
      AND status = 'ACTIVE'
    ORDER BY dot_number
    LIMIT 5
""")

results["fleet_zero"] = {
    "total": fleet_zero_total,
    "active_forhire": fleet_zero_active_forhire,
    "examples": fleet_zero_examples,
}
print(f"  Fleet 0/0 total: {fleet_zero_total:,} | active for-hire: {fleet_zero_active_forhire:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 2 — MC#MC placeholder
# ══════════════════════════════════════════════════════════════════
print("\nCheck 2: MC# placeholder...", flush=True)

mc_placeholder = q(conn, """
    SELECT COUNT(*)
    FROM carriers
    WHERE mc_number = 'MC'
       OR mc_number = 'MC '
""")[0][0]

mc_placeholder_examples = q(conn, """
    SELECT dot_number, legal_name, mc_number, cargo_type
    FROM carriers
    WHERE mc_number = 'MC'
    ORDER BY dot_number
    LIMIT 5
""")

results["mc_placeholder"] = {
    "total": mc_placeholder,
    "examples": mc_placeholder_examples,
}
print(f"  MC# placeholder: {mc_placeholder:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 3 — Duplicate revocation rows
# ══════════════════════════════════════════════════════════════════
print("\nCheck 3: Duplicate revocations...", flush=True)

dup_revoc_groups = q(conn, """
    SELECT COUNT(*) FROM (
        SELECT dot_number, COALESCE(event_date::text,''), COALESCE(description,'')
        FROM carrier_alerts
        WHERE event_type = 'INVOLUNTARY_REVOCATION'
        GROUP BY 1,2,3
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_revoc_rows = q(conn, """
    SELECT COALESCE(SUM(cnt - 1), 0) FROM (
        SELECT COUNT(*) AS cnt
        FROM carrier_alerts
        WHERE event_type = 'INVOLUNTARY_REVOCATION'
        GROUP BY dot_number, COALESCE(event_date::text,''), COALESCE(description,'')
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_revoc_dots = q(conn, """
    SELECT COUNT(DISTINCT dot_number) FROM (
        SELECT dot_number
        FROM carrier_alerts
        WHERE event_type = 'INVOLUNTARY_REVOCATION'
        GROUP BY dot_number, COALESCE(event_date::text,''), COALESCE(description,'')
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_revoc_examples = q(conn, """
    SELECT DISTINCT dot_number
    FROM carrier_alerts
    WHERE event_type = 'INVOLUNTARY_REVOCATION'
    GROUP BY dot_number, COALESCE(event_date::text,''), COALESCE(description,'')
    HAVING COUNT(*) > 1
    LIMIT 5
""")

results["dup_revocations"] = {
    "dup_groups": dup_revoc_groups,
    "extra_rows": dup_revoc_rows,
    "affected_dots": dup_revoc_dots,
    "examples": dup_revoc_examples,
}
print(f"  Dup revocation groups: {dup_revoc_groups:,} | extra rows: {dup_revoc_rows:,} | DOTs: {dup_revoc_dots:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 4 — Duplicate insurance rows
# ══════════════════════════════════════════════════════════════════
print("\nCheck 4: Duplicate insurance rows...", flush=True)

dup_ins_groups = q(conn, """
    SELECT COUNT(*) FROM (
        SELECT dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,'')
        FROM insurance
        GROUP BY 1,2,3
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_ins_rows = q(conn, """
    SELECT COALESCE(SUM(cnt - 1), 0) FROM (
        SELECT COUNT(*) AS cnt
        FROM insurance
        GROUP BY dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,'')
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_ins_dots = q(conn, """
    SELECT COUNT(DISTINCT dot_number) FROM (
        SELECT dot_number
        FROM insurance
        GROUP BY dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,'')
        HAVING COUNT(*) > 1
    ) t
""")[0][0]

dup_ins_examples = q(conn, """
    SELECT DISTINCT dot_number
    FROM insurance
    GROUP BY dot_number, COALESCE(policy_number,''), COALESCE(effective_date::text,'')
    HAVING COUNT(*) > 1
    LIMIT 5
""")

results["dup_insurance"] = {
    "dup_groups": dup_ins_groups,
    "extra_rows": dup_ins_rows,
    "affected_dots": dup_ins_dots,
    "examples": dup_ins_examples,
}
print(f"  Dup insurance groups: {dup_ins_groups:,} | extra rows: {dup_ins_rows:,} | DOTs: {dup_ins_dots:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 5 — Bogus/null-defaulted dates in carrier_alerts
# ══════════════════════════════════════════════════════════════════
print("\nCheck 5: Bogus dates in carrier_alerts...", flush=True)

today = date.today()
week_start = today - timedelta(days=today.weekday())
week_end   = week_start + timedelta(days=6)

null_event_dates = q(conn, """
    SELECT COUNT(*), event_type
    FROM carrier_alerts
    WHERE event_date IS NULL
    GROUP BY event_type
""")

epoch_event_dates = q(conn, """
    SELECT COUNT(*), event_type
    FROM carrier_alerts
    WHERE event_date = '1970-01-01'
    GROUP BY event_type
""")

current_week_alerts = q(conn, """
    SELECT COUNT(*), event_type, MIN(event_date), MAX(event_date)
    FROM carrier_alerts
    WHERE event_date BETWEEN %s AND %s
    GROUP BY event_type
""", (week_start, week_end))

current_week_examples = q(conn, """
    SELECT dot_number, event_type, event_date, description
    FROM carrier_alerts
    WHERE event_date BETWEEN %s AND %s
    ORDER BY event_date DESC
    LIMIT 5
""", (week_start, week_end))

results["bogus_dates"] = {
    "null_event_dates": null_event_dates,
    "epoch_event_dates": epoch_event_dates,
    "current_week_alerts": current_week_alerts,
    "current_week_examples": current_week_examples,
    "week_start": week_start,
    "week_end": week_end,
}
null_count = sum(r[0] for r in null_event_dates)
epoch_count = sum(r[0] for r in epoch_event_dates)
week_count  = sum(r[0] for r in current_week_alerts)
print(f"  Null event_date: {null_count:,} | Epoch (1970): {epoch_count:,} | Current week: {week_count:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 6 — Risk label distribution (computed, not stored)
# ══════════════════════════════════════════════════════════════════
print("\nCheck 6: Risk label distribution (from SMS alerts + crashes)...", flush=True)

# HIGH RISK = 3+ SMS alerts OR any fatal crash
# ELEVATED  = 1-2 SMS alerts OR any crash
# CLEAR     = no alerts, no crashes
# This requires joining sms_scores + crashes — use a CTE

risk_distribution = q(conn, """
    WITH alert_counts AS (
        SELECT
            s.dot_number,
            (COALESCE(s.unsafe_driving_alert::int, 0)
             + COALESCE(s.hours_of_service_compliance_alert::int, 0)
             + COALESCE(s.driver_fitness_alert::int, 0)
             + COALESCE(s.controlled_substances_alcohol_alert::int, 0)
             + COALESCE(s.vehicle_maintenance_alert::int, 0)
             + COALESCE(s.hazardous_materials_alert::int, 0)
             + COALESCE(s.crash_indicator_alert::int, 0)) AS alert_count
        FROM sms_scores s
    ),
    fatal_crashes AS (
        SELECT dot_number, SUM(fatal) AS total_fatal
        FROM crashes
        WHERE crash_date > '1970-01-01'
        GROUP BY dot_number
    ),
    labeled AS (
        SELECT
            a.dot_number,
            CASE
                WHEN a.alert_count >= 3 OR COALESCE(f.total_fatal, 0) > 0 THEN 'HIGH RISK'
                WHEN a.alert_count >= 1 THEN 'ELEVATED'
                ELSE 'CLEAR'
            END AS risk_label
        FROM alert_counts a
        LEFT JOIN fatal_crashes f ON f.dot_number = a.dot_number
    )
    SELECT risk_label, COUNT(*) FROM labeled GROUP BY risk_label ORDER BY COUNT(*) DESC
""")

# Carriers with SMS scores that WOULD show wrong risk (alerts=0 but actually have fatal crash)
hidden_high_risk = q(conn, """
    SELECT COUNT(DISTINCT c.dot_number)
    FROM crashes c
    JOIN sms_scores s ON s.dot_number = c.dot_number
    WHERE c.fatal > 0
      AND c.crash_date > '1970-01-01'
      AND (s.unsafe_driving_alert = false OR s.unsafe_driving_alert IS NULL)
      AND (s.crash_indicator_alert = false OR s.crash_indicator_alert IS NULL)
      AND (s.driver_fitness_alert = false OR s.driver_fitness_alert IS NULL)
      AND (s.vehicle_maintenance_alert = false OR s.vehicle_maintenance_alert IS NULL)
      AND (s.hours_of_service_compliance_alert = false OR s.hours_of_service_compliance_alert IS NULL)
""")[0][0]

risk_examples = q(conn, """
    WITH alert_counts AS (
        SELECT dot_number,
            (COALESCE(unsafe_driving_alert::int, 0)
             + COALESCE(hours_of_service_compliance_alert::int, 0)
             + COALESCE(driver_fitness_alert::int, 0)
             + COALESCE(vehicle_maintenance_alert::int, 0)
             + COALESCE(crash_indicator_alert::int, 0)) AS alerts
        FROM sms_scores
    )
    SELECT a.dot_number, c.legal_name, a.alerts
    FROM alert_counts a
    JOIN carriers c ON c.dot_number = a.dot_number
    WHERE a.alerts >= 3
    ORDER BY a.alerts DESC
    LIMIT 5
""")

results["risk_labels"] = {
    "distribution": risk_distribution,
    "hidden_high_risk": hidden_high_risk,
    "examples": risk_examples,
}
print(f"  Risk distribution: {risk_distribution}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 7 — Null SMS scores
# ══════════════════════════════════════════════════════════════════
print("\nCheck 7: Null SMS scores...", flush=True)

# Carriers IN sms_scores table with at least one null score field
null_sms_any = q(conn, """
    SELECT COUNT(*)
    FROM sms_scores
    WHERE unsafe_driving IS NULL
       OR hours_of_service_compliance IS NULL
       OR driver_fitness IS NULL
       OR controlled_substances_alcohol IS NULL
       OR vehicle_maintenance IS NULL
""")[0][0]

# Carriers with ALL score fields null (would show all "Not Available")
null_sms_all = q(conn, """
    SELECT COUNT(*)
    FROM sms_scores
    WHERE unsafe_driving IS NULL
      AND hours_of_service_compliance IS NULL
      AND driver_fitness IS NULL
      AND controlled_substances_alcohol IS NULL
      AND vehicle_maintenance IS NULL
""")[0][0]

# How many carriers have sms_scores = 0 (stored as 0, could render as "0th percentile")
zero_sms = q(conn, """
    SELECT COUNT(*)
    FROM sms_scores
    WHERE (unsafe_driving = 0 OR unsafe_driving IS NULL)
      AND (hours_of_service_compliance = 0 OR hours_of_service_compliance IS NULL)
      AND (driver_fitness = 0 OR driver_fitness IS NULL)
      AND (vehicle_maintenance = 0 OR vehicle_maintenance IS NULL)
      AND (crash_indicator = 0 OR crash_indicator IS NULL)
""")[0][0]

# Total carriers with NO sms_scores row at all
total_sms = q(conn, "SELECT COUNT(*) FROM sms_scores")[0][0]
carriers_without_sms = total_carriers - total_sms

null_sms_examples = q(conn, """
    SELECT s.dot_number, c.legal_name, s.unsafe_driving, s.vehicle_maintenance, s.crash_indicator
    FROM sms_scores s
    JOIN carriers c ON c.dot_number = s.dot_number
    WHERE s.unsafe_driving IS NULL OR s.vehicle_maintenance IS NULL
    LIMIT 5
""")

results["null_sms"] = {
    "total_with_sms": total_sms,
    "carriers_without_sms": carriers_without_sms,
    "null_any_field": null_sms_any,
    "null_all_fields": null_sms_all,
    "zero_all_fields": zero_sms,
    "examples": null_sms_examples,
}
print(f"  Carriers without SMS: {carriers_without_sms:,} | In table but null fields: {null_sms_any:,} | All-zero: {zero_sms:,}", flush=True)

# ══════════════════════════════════════════════════════════════════
# CHECK 8 — Unmapped CFR codes in violations
# ══════════════════════════════════════════════════════════════════
print("\nCheck 8: Unmapped CFR codes...", flush=True)

# Load cfr_descriptions.json to get known keys
cfr_keys = set()
if os.path.exists(CFR_JSON):
    with open(CFR_JSON, "r", encoding="utf-8") as f:
        cfr_data = json.load(f)
    cfr_keys = set(cfr_data.keys())
    print(f"  cfr_descriptions.json: {len(cfr_keys):,} known codes", flush=True)
else:
    print(f"  WARNING: cfr_descriptions.json not found at {CFR_JSON}", flush=True)

# Get all distinct CFR codes from violations table
all_cfr_codes = q(conn, """
    SELECT description, COUNT(*) AS cnt
    FROM violations
    WHERE description IS NOT NULL
      AND description ~ '^[0-9]'
    GROUP BY description
    ORDER BY cnt DESC
    LIMIT 2000
""")

total_cfr_rows = sum(r[1] for r in all_cfr_codes)
unmapped = [(code, cnt) for code, cnt in all_cfr_codes if code not in cfr_keys]
unmapped_rows = sum(cnt for _, cnt in unmapped)
unmapped_distinct = len(unmapped)

# Also count rows where description IS NULL
null_description = q(conn, "SELECT COUNT(*) FROM violations WHERE description IS NULL")[0][0]
total_violations = q(conn, "SELECT COUNT(*) FROM violations")[0][0]

results["cfr_codes"] = {
    "total_violations": total_violations,
    "null_description": null_description,
    "total_cfr_rows": total_cfr_rows,
    "cfr_keys_in_json": len(cfr_keys),
    "unmapped_distinct": unmapped_distinct,
    "unmapped_rows": unmapped_rows,
    "top_unmapped": unmapped[:10],
}
print(f"  Total violations: {total_violations:,} | Unmapped CFR codes: {unmapped_distinct:,} | Unmapped rows: {unmapped_rows:,}", flush=True)

conn.close()

# ══════════════════════════════════════════════════════════════════
# WRITE REPORT
# ══════════════════════════════════════════════════════════════════
print("\nWriting report...", flush=True)

def pct(n, total=total_carriers):
    return f"{n/total*100:.1f}%" if total else "N/A"

def dot_table(rows, columns):
    if not rows:
        return "_No examples found._\n"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, sep]
    for r in rows:
        lines.append("| " + " | ".join(str(v) if v is not None else "—" for v in r) + " |")
    return "\n".join(lines) + "\n"

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    f.write(f"# Data Quality Validation Report\n")
    f.write(f"Generated: {date.today()} | Total carriers in DB: {total_carriers:,}\n\n")
    f.write("---\n\n")

    # Summary table (filled at end)
    summary_rows = []

    # ── Check 1 ──────────────────────────────────────────────────
    f.write("## 1. Fleet Count 0/0 (Drivers & Trucks Both Zero)\n\n")
    r = results["fleet_zero"]
    f.write(f"| Metric | Count | % of All Carriers |\n")
    f.write(f"|--------|-------|-------------------|\n")
    f.write(f"| Carriers with 0 drivers AND 0 trucks | {r['total']:,} | {pct(r['total'])} |\n")
    f.write(f"| — of which: ACTIVE + for-hire (highest risk) | {r['active_forhire']:,} | {pct(r['active_forhire'])} |\n\n")
    f.write("**5 example DOTs (active carriers with 0/0 fleet):**\n\n")
    f.write(dot_table(r['examples'], ["DOT", "Legal Name", "Status", "Cargo Type"]))
    f.write("\n> **Impact:** These carriers show ⚠ fleet warning in the UI but the underlying data is wrong. Active for-hire carriers should have fleet data.\n\n")
    summary_rows.append(("Fleet 0/0 (all)", r['total'], pct(r['total'])))
    summary_rows.append(("Fleet 0/0 (active for-hire)", r['active_forhire'], pct(r['active_forhire'])))

    # ── Check 2 ──────────────────────────────────────────────────
    f.write("---\n\n## 2. MC# Placeholder Bug (`mc_number = 'MC'`)\n\n")
    r = results["mc_placeholder"]
    f.write(f"| Metric | Count | % of All Carriers |\n")
    f.write(f"|--------|-------|-------------------|\n")
    f.write(f"| Carriers with mc_number = 'MC' (bare prefix) | {r['total']:,} | {pct(r['total'])} |\n\n")
    f.write("**5 example DOTs:**\n\n")
    f.write(dot_table(r['examples'], ["DOT", "Legal Name", "mc_number", "Cargo Type"]))
    f.write("\n> **Impact:** These carriers display 'MC #MC' or blank MC# in the report header. The real MC number was not parsed from the FMCSA Carrier dataset. Fix: re-run `fix_mc_and_fleet.py`.\n\n")
    summary_rows.append(("MC# placeholder ('MC')", r['total'], pct(r['total'])))

    # ── Check 3 ──────────────────────────────────────────────────
    f.write("---\n\n## 3. Duplicate Revocation Rows\n\n")
    r = results["dup_revocations"]
    f.write(f"| Metric | Count |\n")
    f.write(f"|--------|-------|\n")
    f.write(f"| Duplicate (dot_number, event_date, description) groups | {r['dup_groups']:,} |\n")
    f.write(f"| Extra rows to remove | {r['extra_rows']:,} |\n")
    f.write(f"| Affected carriers (DOTs) | {r['affected_dots']:,} | \n\n")
    f.write("**5 example DOTs with duplicate revocation rows:**\n\n")
    f.write(dot_table(r['examples'], ["DOT"]))
    f.write("\n> **Impact:** Revocation history table shows duplicate events. The UI already deduplicates by event_date in component, but raw data is inflated. Fix: `dedup_carrier_alerts.py` (already run Jun 20).\n\n")
    summary_rows.append((f"Dup revocation rows (extra)", r['extra_rows'], f"{r['affected_dots']:,} DOTs"))

    # ── Check 4 ──────────────────────────────────────────────────
    f.write("---\n\n## 4. Duplicate Insurance Rows\n\n")
    r = results["dup_insurance"]
    f.write(f"| Metric | Count |\n")
    f.write(f"|--------|-------|\n")
    f.write(f"| Duplicate (dot_number, policy_number, effective_date) groups | {r['dup_groups']:,} |\n")
    f.write(f"| Extra rows to remove | {r['extra_rows']:,} |\n")
    f.write(f"| Affected carriers (DOTs) | {r['affected_dots']:,} |\n\n")
    f.write("**5 example DOTs with duplicate insurance rows:**\n\n")
    f.write(dot_table(r['examples'], ["DOT"]))
    f.write("\n> **Impact:** Insurance history may show duplicate policy lines. UI deduplicates in component (`dedupedInsurance` IIFE) but raw data is inflated. Fix: `dedup_insurance.py` (confirmed 0 dupes after Jun 20 run).\n\n")
    summary_rows.append((f"Dup insurance rows (extra)", r['extra_rows'], f"{r['affected_dots']:,} DOTs"))

    # ── Check 5 ──────────────────────────────────────────────────
    f.write("---\n\n## 5. Bogus / Null-Defaulted Dates in carrier_alerts\n\n")
    r = results["bogus_dates"]
    null_total = sum(row[0] for row in r['null_event_dates'])
    epoch_total = sum(row[0] for row in r['epoch_event_dates'])
    week_total  = sum(row[0] for row in r['current_week_alerts'])
    f.write(f"| Issue | Count |\n")
    f.write(f"|-------|-------|\n")
    f.write(f"| Rows with NULL event_date | {null_total:,} |\n")
    f.write(f"| Rows with event_date = 1970-01-01 (epoch placeholder) | {epoch_total:,} |\n")
    f.write(f"| Rows with event_date in current week ({r['week_start']} – {r['week_end']}) | {week_total:,} |\n\n")
    if r['null_event_dates']:
        f.write("**NULL event_date breakdown by event_type:**\n\n")
        f.write(dot_table(r['null_event_dates'], ["Count", "event_type"]))
        f.write("\n")
    if r['current_week_alerts']:
        f.write(f"**Current-week alert breakdown (possible null-defaulted to today):**\n\n")
        f.write(dot_table(r['current_week_alerts'], ["Count", "event_type", "Min Date", "Max Date"]))
        f.write("\n")
    if r['current_week_examples']:
        f.write("**5 example current-week alerts:**\n\n")
        f.write(dot_table(r['current_week_examples'], ["DOT", "event_type", "event_date", "description"]))
        f.write("\n")
    f.write("> **Impact:** NULL or epoch event_dates are excluded from all time-bucket views (`isValidDate()` guard). Current-week dates may represent null values defaulted to today in the OOS orders import. Investigate `oos_orders` specifically.\n\n")
    summary_rows.append(("Null event_date in alerts", null_total, "excluded from display"))
    summary_rows.append(("Epoch (1970) event_date in alerts", epoch_total, "excluded from display"))

    # ── Check 6 ──────────────────────────────────────────────────
    f.write("---\n\n## 6. Risk Label Distribution (SMS Alerts + Fatal Crashes)\n\n")
    r = results["risk_labels"]
    f.write("> Note: Risk label is computed client-side, not stored in DB. This shows the computed distribution for all carriers that have SMS scores.\n\n")
    f.write(f"| Risk Label | Count | % of SMS-scored carriers |\n")
    f.write(f"|------------|-------|-------------------------|\n")
    sms_total = sum(row[1] for row in r['distribution'])
    for label, count in r['distribution']:
        f.write(f"| {label} | {count:,} | {count/sms_total*100:.1f}% |\n")
    f.write(f"\n**Carriers with fatal crashes but ALL SMS alerts = false: {r['hidden_high_risk']:,}**\n")
    f.write("(These would be labelled CLEAR by SMS alone but should be HIGH RISK due to fatal crash history.)\n\n")
    f.write("**5 highest-alert carriers (by SMS alert count):**\n\n")
    f.write(dot_table(r['examples'], ["DOT", "Legal Name", "Alert Count"]))
    f.write("\n> **Impact:** Risk badge logic currently requires SMS data. Carriers without SMS scores always show CLEAR regardless of crash history — the `smsAlerts` term is 0 if `sms` prop is null.\n\n")
    summary_rows.append(("HIGH RISK carriers (SMS+crash)", next((cnt for lbl, cnt in r['distribution'] if lbl == 'HIGH RISK'), 0), f"of {sms_total:,} with SMS"))
    summary_rows.append(("Fatal crash but no SMS data (hidden risk)", r['hidden_high_risk'], pct(r['hidden_high_risk'])))

    # ── Check 7 ──────────────────────────────────────────────────
    f.write("---\n\n## 7. Null / Missing SMS Scores\n\n")
    r = results["null_sms"]
    f.write(f"| Metric | Count | % of All Carriers |\n")
    f.write(f"|--------|-------|-------------------|\n")
    f.write(f"| Carriers with NO sms_scores row at all | {r['carriers_without_sms']:,} | {pct(r['carriers_without_sms'])} |\n")
    f.write(f"| In sms_scores but ≥1 null score field | {r['null_any_field']:,} | — |\n")
    f.write(f"| In sms_scores but ALL fields null | {r['null_all_fields']:,} | — |\n")
    f.write(f"| In sms_scores with ALL fields = 0 | {r['zero_all_fields']:,} | — |\n\n")
    f.write("**5 example carriers with null score fields:**\n\n")
    f.write(dot_table(r['examples'], ["DOT", "Legal Name", "unsafe_driving", "vehicle_maintenance", "crash_indicator"]))
    f.write("\n> **Impact:** NULL scores show 'Not Available' (fixed Jun 16). Zero scores also show 'Not Available' (fixed Jun 16 — `hasScore = value > 0`). Carriers with no row show 'No SMS scores published by FMCSA' message. This check confirms the fix is working.\n\n")
    summary_rows.append(("No SMS row (never scored)", r['carriers_without_sms'], pct(r['carriers_without_sms'])))
    summary_rows.append(("SMS row with null fields", r['null_any_field'], "display: 'Not Available' ✓"))

    # ── Check 8 ──────────────────────────────────────────────────
    f.write("---\n\n## 8. Unmapped CFR Codes in Violations\n\n")
    r = results["cfr_codes"]
    mapped_rows = r['total_cfr_rows'] - r['unmapped_rows']
    f.write(f"| Metric | Count |\n")
    f.write(f"|--------|-------|\n")
    f.write(f"| Total violations | {r['total_violations']:,} |\n")
    f.write(f"| Violations with NULL description | {r['null_description']:,} |\n")
    f.write(f"| Known codes in cfr_descriptions.json | {r['cfr_keys_in_json']:,} |\n")
    f.write(f"| Distinct CFR codes scanned (top 2000) | {r['total_cfr_rows']:,} rows |\n")
    f.write(f"| **Unmapped distinct codes** | **{r['unmapped_distinct']:,}** |\n")
    f.write(f"| **Unmapped violation rows** | **{r['unmapped_rows']:,}** |\n")
    f.write(f"| Mapped violation rows | {mapped_rows:,} |\n\n")
    if r['top_unmapped']:
        f.write("**Top 10 unmapped CFR codes (by row count):**\n\n")
        f.write(dot_table(r['top_unmapped'], ["CFR Code", "Row Count"]))
        f.write("\n")
    f.write("> **Impact:** Unmapped codes show `description unavailable` in the UI. The cfr_descriptions.json key format is `'393.9A-LIL'` but DB stores `'393.9(a)'` — a format mismatch means many codes that EXIST in the JSON still fail lookup. Fix requires key normalization in `cfrDescription()` function.\n\n")
    summary_rows.append(("Unmapped CFR violation codes (rows)", r['unmapped_rows'], f"{r['unmapped_distinct']:,} distinct codes"))

    # ── Summary Table ─────────────────────────────────────────────
    f.write("---\n\n## Summary — Prioritised by Impact\n\n")
    f.write("| # | Issue | Affected Count | Scale |\n")
    f.write("|---|-------|---------------|-------|\n")
    # Sort by numeric count where possible
    def sort_key(row):
        try:
            return -int(str(row[1]).replace(",", ""))
        except:
            return 0
    for i, (issue, count, scale) in enumerate(sorted(summary_rows, key=sort_key), 1):
        count_str = f"{count:,}" if isinstance(count, int) else str(count)
        f.write(f"| {i} | {issue} | {count_str} | {scale} |\n")

    f.write(f"\n\n---\n*Report generated {date.today()} against Supabase project `linlnqrroavcutfpmkiz`.*\n")

print(f"\nReport written to: {REPORT_PATH}", flush=True)
print("Done.", flush=True)
