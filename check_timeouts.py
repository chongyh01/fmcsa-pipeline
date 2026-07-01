import os, psycopg2
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=30)
cur = conn.cursor()
cur.execute("SHOW statement_timeout")
print("statement_timeout:", cur.fetchone()[0])
cur.execute("SHOW lock_timeout")
print("lock_timeout:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM authority_history")
print("Current row count:", cur.fetchone()[0])
cur.close()
conn.close()
