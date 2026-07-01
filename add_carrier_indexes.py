import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)
conn.autocommit = True
cur = conn.cursor()

indexes = [
    ("carriers_address_lower_idx", "CREATE INDEX IF NOT EXISTS carriers_address_lower_idx ON carriers(LOWER(TRIM(address)))"),
    ("carriers_phone_idx", "CREATE INDEX IF NOT EXISTS carriers_phone_idx ON carriers(phone)"),
    ("boc3_company_dot_idx", "CREATE INDEX IF NOT EXISTS boc3_company_dot_idx ON boc3(company_name, dot_number)"),
]

for name, sql in indexes:
    print(f"Creating {name}...", flush=True)
    cur.execute(sql)
    print(f"  Done.", flush=True)

# Verify
cur.execute("SELECT indexname, tablename FROM pg_indexes WHERE indexname IN ('carriers_address_lower_idx','carriers_phone_idx','boc3_company_dot_idx')")
rows = cur.fetchall()
for r in rows:
    print(f"  CONFIRMED: {r[1]}.{r[0]}", flush=True)

conn.close()
print("All indexes created.", flush=True)
