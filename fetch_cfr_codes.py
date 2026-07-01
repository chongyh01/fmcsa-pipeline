"""
fetch_cfr_codes.py
==================
Fetch FMCSA violation code → plain-English description from the official
FMCSA Vehicle Inspections and Violations dataset (876r-jsdb).

Writes to: cfr_descriptions.json

That JSON is loaded by the Next.js app at build time (or can be served as a
static file) to populate the CFR code lookup in CarrierDetailView.tsx.

Usage:
  python fetch_cfr_codes.py

Output format:
  { "393.48-BRAKES": "Brakes must be operative", ... }
"""
import os, sys, time, logging, json
import requests
from dotenv import load_dotenv

load_dotenv()
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

APP_TOKEN  = os.getenv("SOCRATA_APP_TOKEN", "")
DATASET_ID = "876r-jsdb"
ENDPOINT   = f"https://data.transportation.gov/resource/{DATASET_ID}.json"
PAGE_SIZE  = 50_000
OUTPUT     = "cfr_descriptions.json"
HEADERS    = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}


def fetch_all():
    codes = {}
    offset = 0
    page = 1
    while True:
        params = {
            "$select": "viol_code,viol_desc",
            "$group":  "viol_code,viol_desc",
            "$limit":  PAGE_SIZE,
            "$offset": offset,
        }
        for attempt in range(4):
            try:
                r = requests.get(ENDPOINT, params=params, headers=HEADERS, timeout=120)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                if attempt == 3:
                    log.error(f"  Failed at page {page}: {e}")
                    data = []
                    break
                time.sleep(2 ** attempt)
        if not data:
            break
        for row in data:
            code = (row.get("viol_code") or "").strip()
            desc = (row.get("viol_desc") or "").strip()
            if code and desc:
                codes[code] = desc
        log.info(f"  Page {page}: +{len(data):,} pairs ({len(codes):,} unique codes so far)")
        if len(data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        page += 1
    return codes


def main():
    log.info(f"Fetching CFR violation descriptions from dataset {DATASET_ID}...")
    codes = fetch_all()
    log.info(f"Done: {len(codes):,} unique violation codes")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(codes, f, ensure_ascii=False, indent=2, sort_keys=True)
    log.info(f"Written to {OUTPUT}")
    log.info("Next step: copy cfr_descriptions.json to carrier-portal/public/ and update")
    log.info("  CFR_DESCRIPTIONS in CarrierDetailView.tsx to load from that file (or inline top-25 codes).")


if __name__ == "__main__":
    main()
