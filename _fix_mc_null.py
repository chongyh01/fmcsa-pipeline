import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"), connect_timeout=20)
cur = conn.cursor()
cur.execute("UPDATE carriers SET mc_number = NULL WHERE mc_number = 'MC'")
print(f"Nulled out {cur.rowcount} carriers with mc_number='MC'")
conn.commit()
conn.close()
