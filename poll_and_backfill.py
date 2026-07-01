"""
poll_and_backfill.py
====================
Step 1: Poll violations table until row count drops to <= 8,000,000 (dedup complete).
Step 2: Run backfill_inspection_id-V1.py (full run, no --pilot).
Step 3: Print final report.
"""
import os, sys, subprocess, time, psycopg2
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
DB_URL = os.getenv("SUPABASE_DB_URL")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).parent

def connect():
    return psycopg2.connect(DB_URL, connect_timeout=30)

# ============================================================
# STEP 1: Poll until dedup complete
# ============================================================
print("=" * 60)
print("STEP 1: Polling violations table (waiting for dedup to finish)")
print("=" * 60)

poll_interval = 60  # seconds
check_num = 0

while True:
    check_num += 1
    try:
        conn = connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM violations")
        n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NULL")
        null_fk = cur.fetchone()[0]
        conn.close()
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] Check #{check_num}: violations={n:,} | inspection_id NULL={null_fk:,}")

        if n <= 8_000_000:
            print(f"\n[{ts}] Dedup appears complete ({n:,} rows <= 8M threshold). Moving to backfill.")
            break
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Poll error: {e} — retrying in 60s")

    sys.stdout.flush()
    time.sleep(poll_interval)

# ============================================================
# STEP 2: Run backfill
# ============================================================
print()
print("=" * 60)
print("STEP 2: Running backfill_inspection_id-V1.py (full run)")
print("=" * 60)
sys.stdout.flush()

backfill_script = SCRIPT_DIR / "backfill_inspection_id-V1.py"
result = subprocess.run(
    [sys.executable, str(backfill_script)],
    cwd=str(SCRIPT_DIR),
)

if result.returncode != 0:
    print(f"\nERROR: backfill script exited with code {result.returncode}")
    sys.exit(result.returncode)

# ============================================================
# STEP 3: Final report
# ============================================================
print()
print("=" * 60)
print("STEP 3: Final report")
print("=" * 60)

try:
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM violations")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
    filled = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NULL")
    null_count = cur.fetchone()[0]
    conn.close()

    fill_rate = filled / total * 100 if total > 0 else 0
    print(f"  Final violations count:      {total:,}")
    print(f"  inspection_id populated:     {filled:,}  ({fill_rate:.1f}%)")
    print(f"  inspection_id NULL:          {null_count:,}")
    print(f"  Fill rate:                   {fill_rate:.1f}%")
except Exception as e:
    print(f"  Final count query failed: {e}")

print("=" * 60)
print("DONE.")
