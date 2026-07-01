import psycopg2, os
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)
conn.autocommit = False
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")

cur.execute("""
DELETE FROM oos_orders a
USING (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY dot_number, order_date, order_type ORDER BY id
  ) AS rn FROM oos_orders
) b
WHERE a.id = b.id AND b.rn > 1
""")
print(f"oos_orders deleted: {cur.rowcount}")
conn.commit()

cur.execute("""
DELETE FROM boc3 a
USING (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY dot_number, COALESCE(company_name,'') ORDER BY id
  ) AS rn FROM boc3
) b
WHERE a.id = b.id AND b.rn > 1
""")
print(f"boc3 deleted: {cur.rowcount}")
conn.commit()

cur.execute("""
DELETE FROM rejected_insurance a
USING (
  SELECT id, ROW_NUMBER() OVER (
    PARTITION BY dot_number, COALESCE(policy_number,''), rejected_date ORDER BY id
  ) AS rn FROM rejected_insurance
) b
WHERE a.id = b.id AND b.rn > 1
""")
print(f"rejected_insurance deleted: {cur.rowcount}")
conn.commit()

for table in ['oos_orders', 'boc3', 'rejected_insurance']:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    print(f"{table}: {cur.fetchone()[0]:,} rows")

conn.close()
print("Done.")
