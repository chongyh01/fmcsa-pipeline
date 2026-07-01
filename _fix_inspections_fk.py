import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=10)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM violations WHERE inspection_id IS NOT NULL")
v = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM citations WHERE inspection_id IS NOT NULL")
c = cur.fetchone()[0]
print("violations non-null inspection_id:", v)
print("citations non-null inspection_id:", c)

if v == 0 and c == 0:
    cur.execute("ALTER TABLE violations DROP CONSTRAINT violations_inspection_id_fkey")
    cur.execute("ALTER TABLE citations DROP CONSTRAINT citations_inspection_id_fkey")
    print("Dropped both FK constraints.")
else:
    print("ABORT: non-null inspection_id values found, not dropping constraints.")

cur.close()
conn.close()
