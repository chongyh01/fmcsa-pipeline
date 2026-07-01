"""
poll_then_violations.py
=======================
Step 1: Poll until inspections reimport completes (>= 8M rows with real dates).
Step 2: Delete stale violations_insp_cache.json so a fresh one is built
        from the newly-reimported inspections table.
Step 3: Run reimport_violations_fast.py.
Step 4: Post-import dedup check + final counts.
"""

import os, sys, time, subprocess, psycopg2
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_URL = os.getenv("SUPABASE_DB_URL")
CODES_DIR = Path(__file__).parent
CACHE_DIR = CODES_DIR / "fmcsa_cache"
INSP_CACHE = CACHE_DIR / "violations_insp_cache.json"
PROGRESS   = CACHE_DIR / "violations_progress.json"

# ── STEP 1: Poll inspections ──────────────────────────────────────────────────
print("=" * 60)
print("STEP 1: Polling inspections reimport...")
print("=" * 60)

def check_inspections():
    try:
        conn = psycopg2.connect(DB_URL, connect_timeout=30)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '15s'")
        cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date > '1970-01-01'")
        real = cur.fetchone()[0]
        conn.close()
        return real
    except Exception as e:
        print(f"  DB check error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None

while True:
    real = check_inspections()
    ts = time.strftime("%H:%M:%S SGT", time.localtime())
    if real is not None:
        print(f"[{ts}] Inspections with real dates: {real:,}")
        if real >= 8_000_000:
            print(f"[{ts}] Inspections reimport COMPLETE ({real:,} rows). Proceeding.")
            break
        else:
            print(f"[{ts}] Still loading ({real:,}/8,000,000). Checking again in 2 min...")
    else:
        print(f"[{ts}] DB unreachable. Retrying in 2 min...")
    time.sleep(120)

# ── STEP 2: Clear stale inspection cache ─────────────────────────────────────
print()
print("=" * 60)
print("STEP 2: Clearing stale inspection cache (will rebuild from DB)")
print("=" * 60)

if INSP_CACHE.exists():
    size_mb = INSP_CACHE.stat().st_size / 1_048_576
    print(f"  Deleting violations_insp_cache.json ({size_mb:.0f} MB, built Jun 9 — pre-reimport)")
    INSP_CACHE.unlink()
    print("  Deleted.")
else:
    print("  No cache found — will build fresh.")

# Ensure no stale progress file
if PROGRESS.exists():
    print(f"  Deleting stale violations_progress.json")
    PROGRESS.unlink()
    print("  Deleted.")

# ── STEP 3: Run violations reimport ──────────────────────────────────────────
print()
print("=" * 60)
print("STEP 3: Running reimport_violations_fast.py ...")
print("=" * 60)

script = CODES_DIR / "reimport_violations_fast.py"
result = subprocess.run(
    [sys.executable, str(script)],
    cwd=str(CODES_DIR),
)
if result.returncode != 0:
    print(f"\nERROR: reimport_violations_fast.py exited with code {result.returncode}")
    sys.exit(result.returncode)

print("\nViolations reimport script finished.")

# ── STEP 4: Post-import dedup check + final counts ───────────────────────────
print()
print("=" * 60)
print("STEP 4: Post-import verification")
print("=" * 60)

try:
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = 0")

    # Total violations
    cur.execute("SELECT COUNT(*) FROM violations")
    total = cur.fetchone()[0]
    print(f"Total violations: {total:,}")

    # Violations with inspection_id
    cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
    with_insp = cur.fetchone()[0]
    print(f"Violations with inspection_id: {with_insp:,}")

    # Dedup check
    print("Running dedup check (may take 1-2 min)...")
    cur.execute("""
        SELECT COUNT(*) as dup_groups FROM (
          SELECT dot_number, violation_code, COALESCE(description,''), COUNT(*) as cnt
          FROM violations
          GROUP BY dot_number, violation_code, COALESCE(description,'')
          HAVING COUNT(*) > 1
        ) sub
    """)
    dup_groups = cur.fetchone()[0]
    print(f"Duplicate groups: {dup_groups:,}")

    pct = (dup_groups / total * 100) if total > 0 else 0
    if dup_groups > 0 and pct > 10:
        print(f"WARNING: {dup_groups:,} dup groups = {pct:.1f}% of total rows — dedup needed!")
    elif dup_groups > 0:
        print(f"Low duplication: {dup_groups:,} groups ({pct:.2f}%) — within acceptable range.")
    else:
        print("No duplicates found.")

    conn.close()

except Exception as e:
    print(f"Post-import check error: {e}")

print()
print("=" * 60)
print("DONE.")
print("=" * 60)
