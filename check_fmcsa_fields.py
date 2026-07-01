import os, sys, requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")
headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}

r = requests.get(
    "https://data.transportation.gov/resource/fx4q-ay7w.json",
    params={"$limit": 5, "$offset": 0},
    headers=headers, timeout=30
)
print("Status:", r.status_code)
rows = r.json()
print(f"FMCSA dataset sample ({len(rows)} rows):")
for i, row in enumerate(rows[:2]):
    print(f"\nRow {i+1} keys: {list(row.keys())}")
    for k, v in row.items():
        if "date" in k.lower() or "insp" in k.lower():
            print(f"  {k}: {v}")
