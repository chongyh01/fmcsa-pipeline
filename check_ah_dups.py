import os, psycopg2
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv('SUPABASE_DB_URL'), connect_timeout=30)
conn.autocommit = True

with conn.cursor() as cur:
    cur.execute('SET statement_timeout = 0')

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT dot_number, COALESCE(status,''), COALESCE(effective_date::text,''), COALESCE(revocation_date::text,''), COUNT(*) as cnt
            FROM authority_history
            GROUP BY 1,2,3,4 HAVING COUNT(*) > 1
        ) sub
    """)
    dups = cur.fetchone()[0]
    print(f'Remaining dup groups in authority_history: {dups:,}', flush=True)

    cur.execute('SELECT COUNT(*) FROM authority_history')
    total = cur.fetchone()[0]
    print(f'Total rows in authority_history: {total:,}', flush=True)

conn.close()
print('Done.', flush=True)
