"""Update carrier safety ratings from FMCSA company_census Socrata dataset."""

import os, sys, requests, psycopg2, psycopg2.extras
from io import StringIO
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RATING_MAP = {
    "S": "Satisfactory", "C": "Conditional",
    "U": "Unsatisfactory", "N": "Not Rated", "NA": "Not Applicable",
}
REVIEW_MAP = {
    "S": "Standard", "V": "Voluntary Safety Evaluation",
    "C": "Compliance Review", "E": "Enforcement",
    "I": "Investigation", "P": "Probationary",
}

def parse_date(val):
    if not val:
        return None
    s = str(val).strip().split(".")[0]
    for fmt in ("%Y%m%d", "%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

token = os.getenv("SOCRATA_APP_TOKEN", "")
headers = {"X-App-Token": token} if token else {}

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
cur = conn.cursor()

offset = 0
page_size = 10000
total_updated = 0

print("Starting safety rating update from Socrata company_census...")

while True:
    r = requests.get(
        "https://data.transportation.gov/resource/az4n-8mr2.csv",
        params={
            "$where": "safety_rating IS NOT NULL",
            "$select": "dot_number,safety_rating,safety_rating_date,review_type,review_date",
            "$limit": str(page_size),
            "$offset": str(offset),
            "$order": ":id",
        },
        headers=headers,
        timeout=60,
    )
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), low_memory=False)
    df.columns = [c.strip().lower() for c in df.columns]

    if len(df) == 0:
        break

    rows = []
    for _, row in df.iterrows():
        dot = str(row.get("dot_number", "")).strip().split(".")[0]
        if not dot or dot in ("nan", "0", ""):
            continue
        rating_raw = str(row.get("safety_rating", "") or "").strip().upper()
        rating = RATING_MAP.get(rating_raw, rating_raw) if rating_raw else None
        review_raw = str(row.get("review_type", "") or "").strip().upper()
        review = REVIEW_MAP.get(review_raw, review_raw) if review_raw else None
        rows.append((
            rating,
            parse_date(row.get("safety_rating_date")),
            review,
            parse_date(row.get("review_date")),
            dot,
        ))

    if rows:
        psycopg2.extras.execute_batch(
            cur,
            """UPDATE carriers SET
               safety_rating=%s, safety_rating_date=%s,
               review_type=%s, review_date=%s
               WHERE dot_number=%s""",
            rows,
            page_size=1000,
        )
        conn.commit()
        total_updated += len(rows)
        print(f"  Updated {total_updated:,} carriers...", flush=True)

    if len(df) < page_size:
        break
    offset += page_size

print(f"DONE. Total carriers updated: {total_updated:,}")
cur.close()
conn.close()
