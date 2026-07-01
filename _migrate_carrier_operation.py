"""
_migrate_carrier_operation.py
==============================
Round 3 schema migration:
  - ADD COLUMN carrier_operation VARCHAR(1)   -- A=Interstate, B=Intrastate HM, C=Intrastate Non-HM
  - ADD COLUMN non_cmv_units     INT          -- non-commercial motor vehicle count (cars/sedans)
  - ADD COLUMN has_passenger_cargo BOOLEAN    -- crgo_passengers flag from FMCSA census

Then UPDATE the 7 Round-3 test DOTs with Socrata-verified values.
Also fix total_trucks for DOT 3128165 (truck_units=0, not power_units=1).

Safe operations only:
  - ADD COLUMN is non-destructive (nullable, no default data change)
  - UPDATE touches 7 specific rows
  - No TRUNCATE, no CASCADE
"""
import os, sys
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
if not DB_URL:
    print("ERROR: SUPABASE_DB_URL not set"); sys.exit(1)

conn = psycopg2.connect(DB_URL, connect_timeout=30)
conn.autocommit = False
cur = conn.cursor()

try:
    # ── Step 1: Add columns if they don't exist ────────────────────────────────
    print("Adding columns (safe to re-run — IF NOT EXISTS guard)...")

    cur.execute("""
        ALTER TABLE carriers
        ADD COLUMN IF NOT EXISTS carrier_operation   VARCHAR(1),
        ADD COLUMN IF NOT EXISTS non_cmv_units       INT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS has_passenger_cargo BOOLEAN NOT NULL DEFAULT FALSE
    """)
    print("  Columns added (or already exist).")

    # ── Step 2: Update the 7 Round-3 test DOTs with Socrata-verified values ────
    print("\nUpdating 7 test DOTs...")

    updates = [
        # (dot_number, carrier_operation, non_cmv_units, has_passenger_cargo, total_trucks_fix)
        # total_trucks_fix = None means no change; otherwise set total_trucks to this value
        ("31047",   "C", 0, False, None),   # AUTH FOR HIRE, Intrastate Non-HM, 1 truck
        ("810652",  "C", 0, False, None),   # EXEMPT FOR HIRE, Intrastate Non-HM, 1 truck
        ("973209",  "C", 0, False, None),   # PRIVATE PROPERTY, Intrastate Non-HM, 2 trucks
        ("4275752", "B", 0, False, None),   # PRIVATE PROPERTY, Intrastate HM, 7 trucks
        # DOT 3128165: power_units=1 but truck_units=0, total_cars=1 → fix total_trucks to 0
        ("3128165", "A", 1, False,  0),     # AUTH FOR HIRE, Interstate, 0 CMV + 1 non-CMV
        # DOT 1864365: crgo_passengers='X', bus_units=10+truck_units=10 → total_trucks stays 20
        ("1864365", "A", 0, True,  None),   # PRIVATE PROPERTY + PASSENGERS, Interstate
        ("2833702", "A", 0, False, None),   # PRIVATE PROPERTY;AUTH FOR HIRE, Interstate
    ]

    for dot, cop, ncmv, pax, trucks_fix in updates:
        if trucks_fix is not None:
            cur.execute("""
                UPDATE carriers
                SET carrier_operation   = %s,
                    non_cmv_units       = %s,
                    has_passenger_cargo = %s,
                    total_trucks        = %s
                WHERE dot_number = %s
            """, (cop, ncmv, pax, trucks_fix, dot))
        else:
            cur.execute("""
                UPDATE carriers
                SET carrier_operation   = %s,
                    non_cmv_units       = %s,
                    has_passenger_cargo = %s
                WHERE dot_number = %s
            """, (cop, ncmv, pax, dot))
        print(f"  DOT {dot}: carrier_operation={cop!r}, non_cmv={ncmv}, "
              f"pax={pax}, trucks={trucks_fix or 'unchanged'} — {cur.rowcount} row(s)")

    conn.commit()
    print("\nMigration committed successfully.")

    # ── Step 3: Verify ─────────────────────────────────────────────────────────
    print("\nVerification:")
    test_dots = [d for d, *_ in updates]
    cur.execute("""
        SELECT dot_number, legal_name, carrier_operation, non_cmv_units,
               has_passenger_cargo, total_trucks
        FROM carriers WHERE dot_number = ANY(%s)
        ORDER BY dot_number
    """, (test_dots,))
    for row in cur.fetchall():
        print(f"  {row[0]:<10} {row[1][:30]:<32} op={row[2]}  "
              f"ncmv={row[3]}  pax={row[4]}  trucks={row[5]}")

except Exception as e:
    conn.rollback()
    print(f"\nERROR — rolled back: {e}")
    raise
finally:
    cur.close()
    conn.close()
