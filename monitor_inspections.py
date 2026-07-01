import psycopg2, os, time
from dotenv import load_dotenv

load_dotenv(r"C:\Users\chong\OneDrive\Documents\Desktop\MISC PROJECT\US DIRECTORY\CARRIER INTELLIGENT REPORT\5 Jun 26 - CARRIER PORTAL\CODES\.env")

DB_URL = os.getenv("SUPABASE_DB_URL")
TARGET = 8158346

conn = psycopg2.connect(DB_URL, connect_timeout=30)
conn.autocommit = True

iteration = 0
while True:
    iteration += 1
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM inspections")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date != '1970-01-01'")
        real = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM inspections WHERE inspection_date = '1970-01-01'")
        epoch = cur.fetchone()[0]
        pct = real / TARGET * 100
        print(f"[Check #{iteration}] Total: {total:,} | Real dates: {real:,} | Epoch: {epoch:,} | Progress: {pct:.1f}%", flush=True)
        cur.close()
        if real >= 8000000:
            print("REIMPORT COMPLETE — real rows >= 8,000,000", flush=True)
            # Final verification
            cur2 = conn.cursor()
            cur2.execute("""
                SELECT inspection_date::text, COUNT(*) as cnt
                FROM inspections
                GROUP BY inspection_date
                ORDER BY COUNT(*) DESC
                LIMIT 5
            """)
            rows = cur2.fetchall()
            print("\nTop 5 inspection_date values:", flush=True)
            for r in rows:
                print(f"  {r[0]}  ->  {r[1]:,} rows", flush=True)
            cur2.close()
            break
    except Exception as e:
        print(f"[Check #{iteration}] ERROR: {e}", flush=True)
        try:
            conn.close()
        except:
            pass
        time.sleep(10)
        conn = psycopg2.connect(DB_URL, connect_timeout=30)
        conn.autocommit = True
        continue

    time.sleep(120)

conn.close()
print("Monitor done.", flush=True)
