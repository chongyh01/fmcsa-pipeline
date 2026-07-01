import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()
db = os.getenv("SUPABASE_DB_URL")
conn = psycopg2.connect(db, connect_timeout=15)
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")

cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date > '1970-01-01'")
real = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date = '1970-01-01'")
epoch = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM inspections")
total = cur.fetchone()[0]
print(f"Total inspections: {total:,}")
print(f"Real dates (>1970): {real:,}")
print(f"Epoch dates (=1970): {epoch:,}")

cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
v = cur.fetchone()[0]
print(f"Violations with inspection_id: {v:,}")

# Check FK constraints on inspections
cur.execute("""
SELECT tc.constraint_name, tc.table_name, kcu.column_name,
       ccu.table_name AS foreign_table_name, ccu.column_name AS foreign_column_name
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND (tc.table_name = 'inspections' OR ccu.table_name = 'inspections')
""")
fks = cur.fetchall()
print("\nFK constraints involving inspections:")
for row in fks:
    print(" ", row)

cur.close()
conn.close()
