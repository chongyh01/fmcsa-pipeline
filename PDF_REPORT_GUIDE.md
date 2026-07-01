# Carrier PDF Report Generator

Script: `generate_pdf.js`  
Location: `CODES/generate_pdf.js`

---

## Quick Start

```bash
# Step 1 — start the portal (leave this terminal open)
cd ../carrier-portal
npm run dev

# Step 2 — in a new terminal, generate the PDF
cd ../CODES
node generate_pdf.js <DOT_NUMBER>
node generate_pdf.js <DOT_NUMBER> <MM/DD/YYYY>
node generate_pdf.js <DOT_NUMBER> <MM/DD/YYYY> "<OUTPUT_FOLDER>"
```

---

## Single Report

```bash
# Basic report (no accident date)
node generate_pdf.js 204814

# With accident date filter
node generate_pdf.js 204814 01/01/2026

# With accident date + custom save folder
node generate_pdf.js 204814 01/01/2026 "C:\Users\chong\Desktop\TEST REPORT"
```

---

## Batch Reports (multiple DOTs at once)

```bash
node generate_pdf.js --batch <DOT1,DOT2,DOT3,...> [MM/DD/YYYY] [OUTPUT_FOLDER]
```

```bash
# 3 DOTs, no accident date
node generate_pdf.js --batch 204814,123456,789012

# 20 DOTs with accident date and custom folder
node generate_pdf.js --batch 204814,623336,1090745,... 01/05/2026 "C:\path\to\TEST REPORT"
```

**Concurrency:** runs 4 Edge instances in parallel. The Next.js dev server can handle 4 concurrent page renders safely — do not raise above 4 or the server will timeout on the later DOTs.

**If some fail with timeout or ERR_CONNECTION_RESET:** just re-run the same command with only the failed DOTs. They will pass on retry once the dev server has recovered. This is normal when generating 15+ PDFs in one batch.

---

## Getting random DOT numbers (Python)

Node.js 24's built-in `fetch()` crashes on Windows — use Python to query the DB instead:

```bash
cd CODES
python3 -c "
import os, sys
from dotenv import load_dotenv
load_dotenv('.env')
import psycopg2
db_url = os.getenv('SUPABASE_DB_URL', '').replace(':6543/', ':5432/')
conn = psycopg2.connect(db_url)
cur = conn.cursor()
cur.execute('SELECT dot_number FROM carriers ORDER BY random() LIMIT 20')
print(','.join(str(r[0]) for r in cur.fetchall()))
conn.close()
"
```

Copy the comma-separated output directly into a `--batch` command.

---

## Output File Naming

| Input | Output filename |
|---|---|
| `204814` (no date) | `DOT_204814.pdf` |
| `204814 01/05/2026` | `DOT_204814_accident_01-05-2026.pdf` |

**Default save folder:** `5 Jun 26 - CARRIER PORTAL\carrier-reports\`  
The output folder is created automatically if it does not exist.

---

## What the Accident Date Does

When you enter an accident date, the report adds a filtered analysis section showing:
- Whether the carrier had **active insurance** on that date
- Whether the carrier had **valid operating authority** on that date
- Any **active revocations** in effect on that date

This matches the "Enter Accident Date" field in the portal UI. Reports with an accident date are typically larger (400–600 KB vs 250 KB) because of the extra analysis sections.

---

## Requirements

| Requirement | Details |
|---|---|
| Node.js | v18+ (tested on v24.15) |
| Puppeteer | Already installed in `CODES/node_modules/` |
| Portal running | `npm run dev` in `carrier-portal/` must be active |
| Portal password | Auto-read from `carrier-portal/.env.local` |

---

## How It Works (under the hood)

1. **Checks** the portal is up using a TCP socket on port 3000 (not `fetch()` — see note below)
2. **Launches** Microsoft Edge in headless mode
3. **Sets** the `cc_auth` session cookie so middleware doesn't redirect to /login
4. **Navigates** to `/carrier/<DOT>` and waits for `networkidle0`
5. **Sets** the accident date via the native DOM value setter + synthetic events (required for React)
6. **Waits** 2s for React to re-render, then another 2s for fonts/content to settle
7. **Prints** to PDF (A4, print background enabled, 10mm margins)
8. **Saves** to the output folder

For batch mode, steps 2–8 run across a pool of 4 concurrent workers.

---

## Fixed Issues — Do Not Redo These

### Puppeteer's bundled Chrome is blocked
Windows Application Control blocks it (`spawn UNKNOWN`). The script uses system Edge at  
`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`. Do not change this.

### Portal requires login
The script reads `SITE_PASSWORD` from `carrier-portal/.env.local`, computes SHA-256, and sets the `cc_auth` cookie before navigating. Without this the browser lands on the login page and the PDF captures a login form.

### Node.js 24 `fetch()` crashes on Windows
Libuv assertion error (`src\win\async.c line 76`). The portal-running check uses `net.createConnection` (raw TCP) instead. Never switch it back to `fetch()`.

### React date input needs native setter
Plain `page.type()` or direct `element.value =` assignment does not fire React's `onChange`. Must use `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set` + dispatch `input` and `change` events.

### JSX syntax bug in CarrierDetailView.tsx (fixed Jun 25 2026)
Line ~1615 had `{(() => {` inside a ternary — Turbopack threw "Expected '</>', got '('". Fixed by removing the outer `{}` wrapper so it reads `(() => {`. Do not reintroduce nested `{(() => {` patterns inside JSX ternaries.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ERROR: carrier-portal is not running` | Run `npm run dev` in `carrier-portal/` first |
| PDF is tiny (< 5 KB) | Portal has a build error — check the dev server terminal output |
| PDF shows the login page | `cc_auth` cookie failed — verify `carrier-portal/.env.local` has `SITE_PASSWORD=...` |
| `spawn UNKNOWN` | Edge path wrong — verify `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` exists |
| Accident date not reflected in PDF | React re-render too slow — increase the 2000ms `setTimeout` after setting the date |
| Batch: some DOTs timeout | Dev server overloaded — re-run with only the failed DOTs, they will pass |
| Batch: all DOTs fail after ~15 | Concurrency too high — `CONCURRENCY` is set to 4 at the top of the script; do not raise it |
