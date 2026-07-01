import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()
db = os.getenv("SUPABASE_DB_URL")
conn = psycopg2.connect(db, connect_timeout=20)
cur = conn.cursor()
cur.execute("SET statement_timeout = '120s'")

tables = ["inspections", "violations", "boc3", "oos_orders", "rejected_insurance",
          "crashes", "carriers", "insurance", "authority_history", "carrier_alerts"]
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"{t}: {cur.fetchone()[0]:,}")

print()
cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date != '1970-01-01'")
print(f"inspections real dates: {cur.fetchone()[0]:,}")
cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date = '1970-01-01'")
print(f"inspections epoch dates: {cur.fetchone()[0]:,}")

cur.close()
conn.close()
print("done")
