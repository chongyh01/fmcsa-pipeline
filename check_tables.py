import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()
db = os.getenv("SUPABASE_DB_URL")
conn = psycopg2.connect(db, connect_timeout=15)
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")

cur.execute("SELECT COUNT(*) FROM violations")
v_total = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
v_with_insp = cur.fetchone()[0]
print(f"violations: {v_total:,} total, {v_with_insp:,} with inspection_id")

try:
    cur.execute("SELECT COUNT(*) FROM citations")
    c_total = cur.fetchone()[0]
    print(f"citations: {c_total:,} total")
except Exception as e:
    print(f"citations table error: {e}")

# Sample violations to understand structure
cur.execute("SELECT id, inspection_id, dot_number, violation_code FROM violations LIMIT 5")
rows = cur.fetchall()
print("\nSample violations rows:")
for r in rows:
    print(" ", r)

# Check inspection_id in violations - are they FKs to inspections.id or something else?
cur.execute("""
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'inspections'
ORDER BY ordinal_position
LIMIT 10
""")
cols = cur.fetchall()
print("\ninspections columns (first 10):")
for c in cols:
    print(" ", c)

cur.close()
conn.close()
