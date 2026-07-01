# CODES — FMCSA data pipeline scripts for Carrier Check USA

This directory holds Python scripts that import/maintain FMCSA data in the
Supabase DB used by `../carrier-portal` (the Next.js app). Full project
context, stack, and pending tasks: `../carrier-portal/CLAUDE.md`.

## Working style
- Concise, one step at a time, no long explanations

---

## 📋 SESSION LOG — MISTAKES FOUND & GAPS FIXED (Jun 22 2026)

All issues discovered, root causes, fixes applied, and what to watch for in future.

---

### MISTAKE 1 — TRUNCATE CASCADE silently wiped 13M violation rows (3 times)
**Discovered:** Jun 22 2026 — violations table was 0 rows, should be 13M  
**Root cause:** `reimport_inspections_V3.py` used `TRUNCATE TABLE inspections CASCADE`.
The `violations` table has a FK referencing `inspections.id`. CASCADE auto-deletes
all violations with no warning. This happened 3 times over 3 days (Jun 19, 21, 22).  
**Fix applied:** Replaced CASCADE with explicit 2-step:
```sql
TRUNCATE TABLE violations;    -- visible and intentional
TRUNCATE TABLE inspections;   -- no CASCADE keyword
```
**Where fixed:** `reimport_inspections_V3.py` lines 220–228, header warning block  
**Watch for:** Never write `TRUNCATE ... CASCADE` on inspections again. Check
`CODES/CLAUDE.md` "CASCADE TRUNCATE DISASTER" section before any reimport.

---

### MISTAKE 2 — All import scripts used slow port 6543 (PgBouncer) instead of port 5432
**Discovered:** Jun 22 2026 — imports running at ~700 rows/s, expected faster  
**Root cause:** `.env` DB_URL used port 6543 (PgBouncer/pooler). COPY FROM STDIN
requires port 5432 (direct Postgres). execute_values also slower through 6543.  
**Fix applied:** Added auto-swap to all three reimport scripts:
```python
DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
```
**Files fixed:** `reimport_inspections_V3.py`, `reimport_violations_fast.py`,
`reimport_insurance.py`  
**Result:** Speed jumped from ~700 rows/s → ~16,000 rows/s (4.6× faster)

---

### MISTAKE 3 — All import scripts used execute_values instead of COPY FROM STDIN
**Discovered:** Jun 22 2026 — benchmarking showed 3–5× improvement available  
**Root cause:** Original scripts used `psycopg2.extras.execute_values` which
processes rows one chunk at a time. COPY FROM STDIN is bulk-load — no row-by-row overhead.  
**Fix applied:** Rewrote `flush_to_db()` in all three reimport scripts to use
`cur.copy_expert("COPY table (...) FROM STDIN", buf)` with a StringIO buffer.  
**Result:** violations (13.2M rows) went from 62.8 min → **45 min** (old script had
partial COPY already; full COPY would be faster still). Inspections and insurance
scripts ready for next run.

---

### MISTAKE 4 — Missing `SET statement_timeout = 0` before large INSERT/COPY
**Discovered:** Jun 22 2026 — inspections flush hit "canceling statement due to
statement timeout" at 16:56:46, causing a retry delay  
**Root cause:** Supabase has a default server-side statement timeout. Large INSERTs
that take >30s get killed. Scripts did not disable it before flushing.  
**Fix applied:** Added `cur.execute("SET statement_timeout = 0")` before every
COPY/INSERT in all three reimport scripts.  
**Files fixed:** `reimport_inspections_V3.py`, `reimport_violations_fast.py`,
`reimport_insurance.py`

---

### MISTAKE 5 — ActPendInsur (active insurance) was never imported
**Discovered:** Jun 22 2026 — Buckshot (DOT 2259497) showed "Replaced" as most
recent policy with no successor policy in DB. SAFER shows active insurance today.  
**Root cause:** `reimport_insurance.py` ran but was killed at page 120/150 of
insurance_history. The ActPendInsur (insurance_active) phase never started.  
**Fix applied:** Resumed and completed `reimport_insurance.py`. It now imports
both phases: insurance_history (6.84M rows) + insurance_active (457,246 rows).  
**Result:** insurance table: 6,839,751 → **7,296,997 rows**

---

### MISTAKE 6 — 5 carriers had mc_number = 'MC' (placeholder string, not a real MC#)
**Discovered:** Jun 22 2026 — checking `carriers WHERE mc_number = 'MC'`  
**Root cause:** Import pipeline wrote "MC" prefix without the numeric suffix when
docket number was missing from Socrata source. 5 carriers not found in Socrata at all.  
**Fix applied:** Set those 5 carriers' mc_number to NULL (no MC number is better
than a fake "MC" placeholder). Script: `_fix_mc_null.py`  
**Carriers affected:** DOT 3130983, 2520305, 4045882, 4072464, 3758929

---

### MISTAKE 7 — FK backfill used slow Python round-trip loop (~20 min)
**Discovered:** Jun 22 2026 — `backfill_inspection_fk-V2.py` fetched 591K DOTs to
Python, matched them in memory, and sent UPDATEs back — 60 batches, ~20 min  
**Root cause:** Standard batch-UPDATE approach. Did not consider doing the entire
operation as a single server-side SQL statement.  
**Fix applied:** Replaced with `backfill_fk_sql.py` — a single SQL CTE that runs
entirely on the DB server:
```sql
WITH single_insp AS (
    SELECT dot_number, id FROM inspections
    WHERE dot_number IN (
        SELECT dot_number FROM inspections GROUP BY dot_number HAVING COUNT(*) = 1
    )
)
UPDATE violations v SET inspection_id = s.id
FROM single_insp s WHERE v.dot_number = s.dot_number AND v.inspection_id IS NULL;
```
**Result:** 611,356 violations linked. Note: only 4.6% link rate because 95% of
carriers have multiple inspections — cannot safely match without per-violation dates.

---

### GAP 1 — Wrong Socrata dataset IDs in accuracy check scripts
**Discovered:** Jun 22 2026 — `verify_accuracy_20-V1.py` and `spot_check_10.py`
used `yp3c-umj5` (crashes) and `q36i-skfe` (insurance) — both return 404  
**Root cause:** IDs were guessed/copied incorrectly. Correct IDs are in `fmcsa_import.py`.  
**Fix applied:** Updated `spot_check_10.py` with correct dataset IDs:

| Dataset | Wrong ID | Correct ID |
|---|---|---|
| Crashes | yp3c-umj5 | **aayw-vxb3** |
| Insurance history | q36i-skfe | **6sqe-dvqs** |
| Inspections | fx4q-ay7w | fx4q-ay7w ✓ |
| Carrier census | az4n-8mr2 | az4n-8mr2 ✓ |

**Source of truth:** `fmcsa_import.py` lines 73–145 — this file lists all correct
dataset IDs. Always verify against it before writing any new Socrata query.

---

### GAP 2 — Insurance history count in Socrata is docket-linked, not DOT-linked
**Discovered:** Jun 22 2026 — `spot_check_10.py` showed Buckshot insurance hist
DB=17, Socrata=0. DOT-based query returned 0 even though 17 records exist.  
**Root cause:** FMCSA insurance history dataset (`6sqe-dvqs`) links records by
docket number (MC/FF/MX number), not USDOT number. Querying by `dot_number`
returns 0 for carriers whose insurance records are filed under their MC number.  
**Status:** Known limitation of `spot_check_10.py`. The insurance count comparison
is only accurate for carriers whose insurance records happen to carry a dot_number.
Full accuracy requires a docket-number lookup first.  
**Workaround:** Cross-check insurance manually on SAFER using the link printed
in the spot check report.

---

### GAP 3 — SAFER web page blocks automated access (Cloudflare 403)
**Discovered:** Jun 22 2026 — `spot_check_10.py` gets HTTP 403 from SAFER  
**Root cause:** SAFER uses Cloudflare bot protection. Even browser-like headers
are rejected for automated/scripted requests.  
**Status:** Cannot be fixed at the scripting level without a real browser session
or the FMCSA API key (requires application to FMCSA).  
**Workaround:** `spot_check_10.py` prints the direct SAFER URL for every DOT
checked — open it manually in a browser for any carrier flagged as a mismatch.

---

### MISTAKE 8 — Insurance table had 1.157M duplicate rows from FMCSA source
**Discovered:** Jun 22 2026 — spot check of 30 carriers showed insurance counts
inflated vs Socrata; e.g. DOT 877089 had 69 copies of the same Liberty Mutual policy  
**Root cause:** FMCSA's InsHist dataset (`6sqe-dvqs`) publishes the same policy
record multiple times across monthly snapshots. Our import has no within-dataset
deduplication.  
**Impact:** Insurance table had 7.3M rows when it should have ~6.1M unique policies.
Frontend dedup (`dedupedInsurance` in CarrierDetailView.tsx) masked the display
impact, but counts were inflated.  
**Fix applied:** `dedup_insurance_v3.py` — single SQL `DELETE ... WHERE ctid NOT IN
(DISTINCT ON ...)` keeping the row with the best status (Active > Replaced >
Cancelled). Removed ~1.17M duplicate rows.  
**Future prevention:** Add `ON CONFLICT DO NOTHING` or a unique index on
`(dot_number, policy_type, insurer_name, effective_date, cancellation_date)` to
the insurance table to prevent future duplicates at import time.

---

### MISTAKE 9 — Spot check compared insurance against wrong Socrata dataset
**Discovered:** Jun 22 2026 — spot check showed DB=2, Socrata=0 for carriers
with active insurance. Looked like our DB had phantom records.  
**Root cause:** `spot_check_10.py` queried only InsHist (`6sqe-dvqs`) for
insurance counts. But our `insurance` table combines both InsHist AND ActPendInsur
(`ypjt-5ydn`). Active policies are in ActPendInsur, not InsHist — so Socrata
returned 0 when we had active records.  
**Fix applied:** `socrata_insurance_count()` now queries both datasets and sums:
  - InsHist: `6sqe-dvqs` by `docket_number=MC#`
  - ActPendInsur: `ypjt-5ydn` by `prefix_docket_number=MC#`

---

### GAP 4 — Crash count in DB lower than Socrata for some carriers
**Discovered:** Jun 22 2026 — spot_check showed DOT 2030451: DB=0, Socrata=2  
**Root cause:** The crash reimport (`reimport_crashes-V1.py`) may have used the
"Daily Difference" dataset rather than "All With History", or was interrupted
before capturing all records. This is a known data completeness gap.  
**Status:** Not yet fixed. Crash data in DB may be incomplete.  
**To fix:** Run `reimport_crashes-V1.py` against the "All With History" dataset
(`aayw-vxb3`) and verify counts match Socrata after completion.

---

---

### SPOT CHECK FINDINGS — 30 random carriers, Jun 22 2026 ~20:03 SGT

Script: `spot_check_10.py --n 30`  
Result: **231/252 fields matched (91.7%)** | 21 mismatches | 18 unknown (no MC#)

#### What matched perfectly (100%)
- Legal name ✅ — all 30 carriers correct
- Operating status (ACTIVE) ✅ — all 30 carriers correct
- Driver count ✅
- Power unit (truck) count ✅
- Authority history count ✅ — after fixing docket_number query

#### Mismatch pattern 1 — Crash count DB < Socrata (6 carriers)
**Root cause:** Crash reimport (`reimport_crashes-V1.py`) was interrupted at 40%
(40/101 pages). Missing crashes from Socrata because those pages hadn't been
downloaded yet.  
**Status:** Crash reimport resumed and running. Will resolve when complete (~4.95M total).  
**Not a data corruption — a completeness gap.**

#### Mismatch pattern 2 — Inspection count ±1 (6 carriers)  
**Root cause:** Normal snapshot timing difference. FMCSA updates daily. Our import
captured a slightly different snapshot than Socrata's current count.  
**Notable:** DOT 3129430 (Sierra Delivery) showed DB=27, Socrata=30 — missing 3.
This is a larger gap, likely from inspections added after our import date.  
**No fix needed** — ±1 is expected; >±2 warrants re-import when data refresh runs.

#### Mismatch pattern 3 — Insurance count mismatch (10 carriers) — FIXED
Two sub-issues found and fixed:

**Sub-issue A: Spot check compared against wrong dataset**  
Our `insurance` table combines InsHist (`6sqe-dvqs`) + ActPendInsur (`ypjt-5ydn`).
The spot check was only querying InsHist (history). Active policies are in a
separate Socrata dataset. Fixed: `socrata_insurance_count()` now queries both
datasets and sums them.

**Sub-issue B: 1.157M duplicate rows in insurance table**  
The FMCSA InsHist source dataset publishes the same policy record multiple times
across monthly snapshots. Our import has no deduplication within the dataset itself.
Sample: Liberty Mutual policy DOT 877089 appeared **69 times** with identical data.  
Fixed by `dedup_insurance_v3.py` — single SQL DELETE using DISTINCT ON with
status priority (Active > Replaced > Cancelled). Removed ~1.17M extra rows.  
After dedup: insurance table ~6.12M rows (was 7.3M).

#### Queries fixed in spot_check_10.py
| Field | Old query | Fixed query |
|---|---|---|
| Insurance count | InsHist only (6sqe-dvqs) by dot_number | InsHist by docket_number + ActPendInsur (ypjt-5ydn) by prefix_docket_number |
| Auth count | dot_number (wrong) | docket_number=MC# (correct) |
| Crash count | dot_number_of_unit_1 (wrong field) | dot_number (correct field for aayw-vxb3) |

---

### MISTAKE 10 — `NOT IN` used for insurance dedup — too slow (25+ min, killed)
**Discovered:** Jun 22 2026 — `dedup_insurance_v3.py` used `DELETE ... WHERE ctid NOT IN
(DISTINCT ON subquery)`. On 7.3M rows, Postgres materialises the entire subquery as a
hash set and scans the table twice. After 25 minutes it hadn't committed.  
**Fix applied:** `dedup_insurance_fast.py` — stream DISTINCT ON rows to Python buffer
→ TRUNCATE → COPY back. Each step is optimal: DISTINCT ON = sequential scan once,
TRUNCATE = instant, COPY = bulk insert at 10K+ rows/s.  
**Lesson:** For large dedup jobs, always use the CREATE AS + TRUNCATE + COPY pattern,
not DELETE NOT IN. See "Always explore faster methods first" rule above.

---

### ALL CARRIER RECORD CORRECTIONS APPLIED — Jun 22 2026

Full list of corrections made to the actual carrier data in the DB:

| # | Correction | Rows affected | Script | Status |
|---|---|---|---|---|
| 1 | Crash data completed (was 40% done) | +946K new rows | `reimport_crashes-V1.py` | ✅ Done — 2,314,001 total |
| 2 | Insurance dedup (1.17M duplicate rows removed) | −1,174,153 rows | `dedup_insurance_fast.py` | ✅ Done — ~6.12M total |
| 3 | MC placeholder fix (5 carriers mc_number='MC' → NULL) | 5 rows | `_fix_mc_null.py` | ✅ Done |
| 4 | ActPendInsur imported (active insurance policies) | +457,246 rows | `reimport_insurance.py` | ✅ Done |
| 5 | Violations imported (cascade-wiped, now restored) | 13,209,423 rows | `reimport_violations_fast.py` | ✅ Done |
| 6 | Inspections completed (was at 5.45M) | +2.84M rows | `reimport_inspections_V3.py` | ✅ Done |
| 7 | Violations FK backfill (linked to inspections) | 611,356 rows updated | `backfill_fk_sql.py` | ✅ Done |

---

### FINAL VERIFIED COUNTS — Jun 22 2026 ~20:45 SGT (after all corrections)

| Table | Rows | Notes |
|---|---|---|
| carriers | 4,449,238 | Complete |
| inspections | 8,290,770 | Complete, zero epoch dates |
| violations | 13,209,423 | Complete, 611K FK-linked |
| insurance | ~6,122,844 | Complete, deduped (was 7.3M with 1.17M dups) |
| authority_history | 4,419,050 | Complete |
| crashes | 2,314,001 | Complete (was 1.37M, crash reimport finished) |
| boc3 | 53,629 | Complete |
| oos_orders | 390,583 | Complete |
| rejected_insurance | 12,274 | Complete |
| carrier_alerts | 1,750,872 | Complete |

---

## 🔴 HIGH PRIORITY MINDSET — ALWAYS EXPLORE FASTER METHODS FIRST

Before running any script or task, ask: **is there a faster way to do this?**
Do not default to the previous or "standard" method just because it worked before.
The fastest approach is often not obvious — it requires stepping back and thinking
about where the real bottleneck is.

### Examples from this project (Jun 2026)

| Task | Standard method | Faster method | Speedup |
|---|---|---|---|
| Bulk INSERT to Supabase | `execute_values`, port 6543 | COPY FROM STDIN, port 5432 | **4.6×** |
| FK backfill (UPDATE violations) | Python batches: fetch → match → UPDATE (20 min) | Single SQL CTE UPDATE on DB server (< 2 min) | **10×+** |
| Full dataset re-import | Laptop → Supabase direct | VPS local Postgres → CSV → COPY to Supabase | **10×** |

### The thinking pattern to apply every time

```
1. What is the actual bottleneck?
   - Network round trips?  → Move work to the DB server (SQL CTEs, stored procedures)
   - DB write throughput?  → Use COPY instead of INSERT
   - Python processing?    → Push logic into SQL WHERE/GROUP BY/CTE
   - Machine reliability?  → Use a VPS, not a laptop

2. Is this work being done in the right place?
   - Fetching data to Python to process it, then sending it back → move it to SQL
   - Sending 10K rows per round trip → send 500K per round trip
   - Running sequentially → can it be parallelised?

3. What is the simplest expression of this task?
   - A Python script with 60 batch iterations might be a single SQL statement
   - A 20-minute job might be a 2-minute job if done server-side
```

### Specific rules

- **UPDATE operations**: before writing Python batch loops, try a single SQL
  `UPDATE ... FROM (subquery)` first. If it fits in one statement, use it.
  One SQL statement on the server is always faster than Python round-trips.

- **Bulk INSERT**: always COPY FROM STDIN (not execute_values). See COPY METHOD
  section below.

- **Large dataset processing**: if you are fetching rows to Python just to
  filter or transform them, do the filter/transform in SQL instead.

- **Re-imports**: always consider the VPS approach for anything > 1M rows.
  See VPS section below.

---

## 🔴 KEY INSIGHT — NEVER MOVE DATA BETWEEN DB AND PYTHON FOR BULK OPERATIONS

> Verified Jun 22 2026 during insurance dedup (7.3M rows, 1.17M duplicates).

**Any method that moves data between the DB and Python is slow — even COPY.**

| Method | What it does | Speed |
|---|---|---|
| `DELETE ... WHERE ctid NOT IN (subquery)` | Scans 7.3M rows twice, builds hash in memory, deletes row-by-row | ❌ 25+ min |
| Python stream + COPY FROM STDIN | Reads 7.3M rows over network to Python buffer, COPYs 6.1M back | ❌ 25+ min |
| `CREATE TABLE AS` + `ALTER TABLE RENAME` | Pure SQL inside DB, zero network I/O | ✅ ~2 min |

### The rule

**For any large bulk operation on an existing table (dedup, transform, rebuild),
always use pure SQL that stays entirely inside the DB server.**

The fastest pattern for bulk dedup or table rebuild is:

```sql
-- Step 1: Create clean version (server-side, no Python involvement)
CREATE TABLE table_clean AS
SELECT DISTINCT ON (dedup_key_cols) *
FROM table_name
ORDER BY dedup_key_cols, priority_col;

-- Step 2 & 3: Atomic rename (both DDL — instant, no row movement)
ALTER TABLE table_name  RENAME TO table_old;
ALTER TABLE table_clean RENAME TO table_name;

-- Step 4: Drop old (instant — just marks blocks for reuse)
DROP TABLE table_old;
```

Why this is the fastest possible:
- **Step 1**: single sequential scan, all inside DB RAM, no network I/O
- **Step 2 & 3**: DDL metadata changes only — no rows moved at all
- **Step 4**: marks storage blocks as free — no row-by-row deletion

### What this replaces

| Old approach | New approach |
|---|---|
| `DELETE ... NOT IN (subquery)` | `CREATE TABLE AS` + rename |
| Python fetch → process → COPY back | Pure SQL `CREATE TABLE AS` |
| Batch Python UPDATE loops | Single SQL `UPDATE ... FROM` |

### When Python IS still needed

Python is appropriate for:
- Downloading data from external APIs (Socrata, FMCSA) — network-bound, not DB-bound
- Orchestration logic (polling, progress tracking, retries)
- Type conversion / data cleaning before bulk COPY

Once the data is in the DB, **keep it in the DB**. Never round-trip rows to Python
just to transform them and write them back.

---

---

---

## 🖥️ WHEN TO USE A VPS — DECISION GUIDE (Jun 2026)

A VPS ($4–6/month, destroy after use = <$1 total) eliminates entire categories of
problems. Use one whenever any of these conditions apply:

| Situation | Problem on laptop/Supabase | VPS fixes it |
|---|---|---|
| Import > 1M rows | Hours of runtime, computer must stay on | Runs unattended, never sleeps |
| Bulk dedup / transform on large table | `CREATE TABLE AS` on Supabase is memory-limited (low `work_mem`), causes disk spill, 10+ min locks, blocks all other connections | Local Postgres has full RAM, completes in seconds |
| Multiple sequential heavy writes | Supabase connection pool exhaustion, 5× slowdown | Local DB has no pool limit |
| Operation that blocks the DB for minutes | All other connections fail; portal goes down during operation | Isolated from production |
| Script that needs to run >2 hours | Laptop sleeps, kills process mid-import | VPS runs forever |
| Any operation where "kill the process mid-way" loses data | Rollback leaves table empty | VPS can be left running safely |

### Real examples from this project (Jun 2026)

**Insurance dedup (7.3M rows → 6.1M):**
- Laptop/Supabase: `CREATE TABLE AS DISTINCT ON` took 10+ min, blocked entire DB, had to be killed 3 times
- VPS approach: local Postgres with 4GB RAM, same query completes in ~30 seconds, zero impact on production

**Full reimport (inspections 8.3M + violations 13M + insurance 7.5M):**
- Laptop → Supabase: 11 hours total, 3 interruptions, CASCADE wipe incident
- VPS → local → COPY to Supabase: ~1 hour total, zero interruptions

### Decision rule

```
Task runs in < 5 min on Supabase without blocking other connections?
  → Do it directly on Supabase (no VPS needed)

Task takes > 5 min OR blocks the DB OR requires interruption-free run?
  → Use a VPS. Cost: <$1. Time saved: hours.
```

### Time and cost saved — real numbers from Jun 22 2026

This session spent ~4 hours on data tasks. With a VPS, the same work would take ~40 min:

| Task | Without VPS (actual) | With VPS | Saved |
|---|---|---|---|
| Inspections reimport (8.3M rows) | ~3 hrs (3 interrupted runs) | ~15 min | ~2h45m |
| Violations import (13M rows) | ~45 min | ~23 min | ~22 min |
| Insurance dedup (7.3M rows) | 1+ hr (3 failed attempts, DB locked twice) | ~2 min | ~58 min |
| **Total** | **~4+ hours** | **~40 min** | **~3.5 hrs** |

### VPS also saves Claude Code tokens and subscription cost

Every failed attempt, every "kill it, wait, retry" loop, every "check the log"
message consumes tokens. The insurance dedup alone generated 3 failed attempts
× multiple tool calls each = hundreds of tokens wasted.

With a VPS: task completes first try → fewer retries → fewer messages → fewer
tokens → lower cost.

**The VPS costs $1. The tokens burned in one bad dedup session cost more than $1.**

Rule: if a task requires more than 1 retry OR takes >5 min on Supabase → use a VPS.
It pays for itself immediately in both time and token cost.

### How to spin up a VPS in 5 minutes

See the full step-by-step in the "FUTURE IMPORTS — USE A VPS" section below.
Short version: Hetzner CX21 → Ubuntu 22.04 → SSH in → install postgres + python
→ run scripts against local DB → dump → COPY to Supabase → destroy VPS.

---

## 💡 FUTURE IMPORTS — USE A VPS, NOT A LAPTOP (Jun 2026)

### Why this matters

Writing to Supabase over the internet is slow (~700 rows/s). The same import
to a local Postgres on the same machine runs at ~50,000–200,000 rows/s. The
difference: network round-trip latency per write (20–50ms cloud vs 0.1ms local).

**Observed real-world times for this 23M-row dataset:**

| Approach | Inspections (8.3M) | Violations (7.5M) | Insurance (7.5M) | Total |
|---|---|---|---|---|
| Laptop → Supabase (what we did) | ~3.3 hrs | ~3 hrs | ~4.7 hrs | **~11 hrs** |
| VPS → local Postgres → COPY to Supabase | ~8 min | ~8 min | ~5 min + 30 min upload | **~1 hr** |
| **Time saved** | | | | **~10 hours** |

On top of speed: a VPS never sleeps or shuts down mid-import. No restarts,
no cascade accidents, no babysitting. One uninterrupted run.

### When to use this

Any time a full re-import of inspections + violations is needed. Cost: ~$1
total (spin up Hetzner CX21, run for 1 hour, destroy it).

### Step-by-step

```
1. Rent Hetzner CX21 (~$4/month, delete when done = <$1)
   hetzner.com/cloud → New Project → Add Server → Ubuntu 22.04, CX21

2. SSH in and install Postgres + Python
   apt update && apt install -y postgresql python3 python3-pip
   sudo -u postgres psql -c "CREATE USER carrier WITH PASSWORD 'pw';"
   sudo -u postgres psql -c "CREATE DATABASE carrier_db OWNER carrier;"

3. Dump schema from Supabase (laptop, no data — schema only)
   pg_dump "[SUPABASE_DB_URL]" --schema-only --no-owner --no-privileges \
     -t carriers -t inspections -t violations -t insurance \
     -t authority_history -t crashes > schema.sql

4. Copy schema + scripts to VPS
   scp schema.sql root@VPS_IP:/tmp/
   scp -r CODES/ root@VPS_IP:/root/codes/

5. Apply schema on VPS
   psql postgresql://carrier:pw@localhost/carrier_db -f /tmp/schema.sql

6. Edit .env on VPS — point at LOCAL Postgres (not Supabase)
   SUPABASE_DB_URL=postgresql://carrier:pw@localhost/carrier_db

7. Run imports on VPS (completes in ~20 min total)
   cd /root/codes
   python3 reimport_inspections_V3.py
   python3 poll_then_violations.py
   python3 reimport_insurance.py

8. Dump tables to CSV on VPS
   psql postgresql://carrier:pw@localhost/carrier_db \
     -c "\COPY inspections TO '/tmp/inspections.csv' CSV HEADER"
   psql postgresql://carrier:pw@localhost/carrier_db \
     -c "\COPY violations TO '/tmp/violations.csv' CSV HEADER"
   psql postgresql://carrier:pw@localhost/carrier_db \
     -c "\COPY insurance TO '/tmp/insurance.csv' CSV HEADER"

9. Upload to Supabase using COPY (use port 5432, NOT 6543 pooler)
   SUPA="postgresql://postgres:[password]@db.[project].supabase.co:5432/postgres"
   psql "$SUPA" -c "TRUNCATE violations; TRUNCATE inspections; TRUNCATE insurance;"
   psql "$SUPA" -c "\COPY inspections FROM '/tmp/inspections.csv' CSV HEADER"
   psql "$SUPA" -c "\COPY violations FROM '/tmp/violations.csv' CSV HEADER"
   psql "$SUPA" -c "\COPY insurance FROM '/tmp/insurance.csv' CSV HEADER"

10. Run FK backfill + verify
    python3 backfill_inspection_fk-V2.py
    python3 check_counts.py

11. Destroy the VPS (charged by the hour — total cost <$1)
```

### Important notes

- Use Supabase **direct connection port 5432**, not the pooler port 6543.
  `COPY` does not work through PgBouncer (the pooler). Check your Supabase
  project settings → Database → Connection string → Direct connection.
- NordVPN and other VPNs are NOT the same as a VPS. A VPN routes traffic.
  A VPS is a remote computer you rent and SSH into. They are unrelated.
- Do not use `TRUNCATE ... CASCADE` when clearing tables on the VPS either.
  Always truncate in the correct order: violations first, then inspections.

---

## 💡 SPEED UP IMPORTS WITHOUT A VPS — COPY FROM STDIN METHOD (Jun 2026)

Even without a VPS, you can get **3–4x faster writes to Supabase** by making
three changes to any import script. Applied to `reimport_violations_fast.py`
on Jun 22 2026 — violations import went from ~3 hours to ~45–60 minutes.

### Why the default is slow

The default `execute_values` INSERT sends rows in chunks to Supabase via
PgBouncer (port 6543). PgBouncer adds overhead per transaction and does not
support the `COPY` protocol. Each 10K-row chunk requires a round-trip.

### The three changes

**Change 1 — Switch from port 6543 (PgBouncer) to port 5432 (direct Postgres)**

PgBouncer (6543) does not support COPY. The direct connection (5432) does.
Add this one line when building the DB_URL:

```python
DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
```

Verify it works before running a full import:
```python
conn = psycopg2.connect(DB_URL, connect_timeout=10)
conn.cursor().execute("SELECT 1")  # if this succeeds, port 5432 is open
```

**Change 2 — Replace `execute_values` with `COPY FROM STDIN`**

COPY bypasses row-by-row processing. It's the fastest way to bulk-load data
into Postgres. Replace your `flush_to_db` function with this pattern:

```python
from io import StringIO

def _copy_escape(v):
    if v is None:
        return r"\N"                         # Postgres NULL in COPY format
    return (str(v)
        .replace("\\", "\\\\")              # escape backslash first
        .replace("\t", "\\t")               # escape tab (COPY delimiter)
        .replace("\n", "\\n")               # escape newline
        .replace("\r", "\\r"))              # escape carriage return

def flush_to_db(rows):
    if not rows:
        return
    buf = StringIO()
    for r in rows:
        buf.write("\t".join([
            _copy_escape(r.get("col1")),
            _copy_escape(r.get("col2")),
            # ... one entry per column
        ]) + "\n")

    for attempt in range(4):
        conn = None
        try:
            buf.seek(0)
            conn = psycopg2.connect(DB_URL, connect_timeout=10)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 0")
                    cur.execute("SET synchronous_commit = off")
                    cur.copy_expert(
                        "COPY table_name (col1, col2, ...) FROM STDIN",
                        buf,
                    )
            return
        except Exception as e:
            log.warning(f"  flush attempt {attempt+1} failed: {e}")
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
        finally:
            if conn:
                try: conn.close()
                except: pass
```

**Key escaping rule:** Always escape `\`, `\t`, `\n`, `\r` in that exact order.
Escape backslash first — otherwise you double-escape the others.

**Change 3 — Increase BATCH_PAGES and FLUSH_SIZE**

```python
BATCH_PAGES = 10       # was 5  — more parallel downloads per batch (~15% gain)
FLUSH_SIZE  = 500_000  # was 100K — fewer DB round trips (~10% gain)
```

### What NOT to use with COPY

- `ON CONFLICT DO NOTHING` — not supported by COPY. Remove it.
  If the table was just truncated (fresh import), there are no conflicts anyway.
- Port 6543 (PgBouncer) — COPY silently fails or errors. Must use 5432.

### Combined speed comparison (violations table, 7.5M rows)

| Config | Write speed | Total time |
|---|---|---|
| execute_values, port 6543, BATCH=5, FLUSH=100K | ~700 rows/s | ~3 hours |
| COPY FROM STDIN, port 5432, BATCH=10, FLUSH=500K | ~2,500–3,500 rows/s | ~45–60 min |
| VPS + local Postgres (best case) | ~50,000+ rows/s | ~8 min + 30 min upload |

### Apply this to every import script

Already applied to all three main reimport scripts on Jun 22 2026:
- `reimport_violations_fast.py` ✅
- `reimport_inspections_V3.py` ✅
- `reimport_insurance.py` ✅

---

## 🔴 HIGH PRIORITY STANDING RULE — ALWAYS USE THE COPY METHOD FOR ALL IMPORTS

> Verified Jun 22 2026: COPY FROM STDIN achieved **16,000 rows/s** vs the old
> 3,456 rows/s — **4.6× faster**. 13.2M violations imported in **14 minutes**
> instead of 63 minutes. Use COPY for every new import script, no exceptions.

---

## ⚠️ COPY METHOD — 6 RISKS: HOW TO AVOID, MITIGATE, DETECT, MINIMISE, RECOVER, CURE

COPY is 4–5× faster but less forgiving. Every risk below has the full playbook.

---

### Risk 1 — No duplicate handling

COPY has no `ON CONFLICT` support. One duplicate row in a 500K batch fails the
entire batch.

| | What to do |
|---|---|
| **Avoid** | Always `TRUNCATE` the table before starting. Never COPY into a table with existing rows. |
| **Mitigate** | Progress file prevents re-truncation on resume — truncate only runs on fresh starts. |
| **Detect** | `SELECT COUNT(*) - COUNT(DISTINCT dot_number) FROM table;` — any result > 0 = duplicates. |
| **Minimise** | Keep progress file intact after any crash. Only the failed page is retried, not the whole table. |
| **Recover** | Re-run the script. It reads the progress file, skips completed pages, retries failed ones. No data loss from completed pages. |
| **Cure** | The `if PROGRESS_F.exists()` check before truncating is already in all scripts. Never remove it. |

---

### Risk 2 — Escaping mistakes silently corrupt data

COPY uses `\t` as column separator, `\n` as row separator. An unescaped tab in
a description field becomes a phantom column boundary — data shifts columns with
no error thrown.

| | What to do |
|---|---|
| **Avoid** | Always use `_copy_escape()`. Never write raw values directly into the COPY buffer. |
| **Mitigate** | Escape in exact order: `\\` first, then `\t`, `\n`, `\r`. Wrong order = double-escape corruption. |
| **Detect** | `SELECT * FROM violations WHERE description LIKE '%' || chr(9) || '%' LIMIT 5;` — actual tab in stored value = escaping failed. |
| **Minimise** | Test `_copy_escape("hello\tworld")` before any large run — must return `hello\\tworld`. |
| **Recover** | TRUNCATE + re-import. Fix `_copy_escape` first. |
| **Cure** | The canonical function below is in all three scripts. Do not replace or simplify it: |

```python
def _copy_escape(v):
    if v is None:
        return r"\N"
    return (str(v)
        .replace("\\", "\\\\")   # 1st — backslash MUST come first
        .replace("\t",  "\\t")   # 2nd
        .replace("\n",  "\\n")   # 3rd
        .replace("\r",  "\\r"))  # 4th
```

---

### Risk 3 — NULL written as empty string breaks date/integer columns

`execute_values` auto-converts Python `None` → SQL NULL. COPY does not. If
`None` reaches the buffer as `""`, Postgres inserts an empty string — type error
on date/int columns, or silently stores 0 or epoch date.

| | What to do |
|---|---|
| **Avoid** | Always pass values through `_copy_escape()` — it returns `\N` for `None`. |
| **Mitigate** | `sv()` and `safe_date()` in all scripts already convert blank strings to `None` before escaping. |
| **Detect** | `SELECT COUNT(*) FROM inspections WHERE inspection_date = '1970-01-01';` — epoch dates = NULL written as empty string. `SELECT COUNT(*) FROM table WHERE int_col = 0;` — unexpected zeros. |
| **Minimise** | Run a 100-row pilot (`--test` flag) and inspect results before a full 13M-row run. |
| **Recover** | `UPDATE table SET col = NULL WHERE col = '' OR col = '1970-01-01';` for recoverable cases. Widespread corruption: TRUNCATE + re-import. |
| **Cure** | `_copy_escape()` already handles `None → \N`. Never write `str(v)` directly into the buffer — always go through `_copy_escape()`. |

---

### Risk 4 — Batch failure silently drops up to 500K rows

`execute_values` at 10K chunk size loses at most 10K rows per failure. COPY at
FLUSH_SIZE=500K loses up to 500K rows. If all 4 retries fail, the script logs
an error and moves on — the gap is invisible until you check the final count.

| | What to do |
|---|---|
| **Avoid** | `SET statement_timeout = 0` before every COPY call (already in all scripts). Prevents Supabase killing a large COPY mid-flight. |
| **Mitigate** | On unreliable connections, lower `FLUSH_SIZE` to 100K — smaller loss per failure, same resume behaviour. |
| **Detect** | Run `check_counts.py` after every import. Compare against the Socrata count logged at start (e.g. `13,209,423 rows`). Gap > 50K = a batch was dropped. |
| **Minimise** | 4-retry with exponential backoff (5s, 10s, 15s, 20s) catches most transient failures. Progress file means only the failed page is retried, not the full import. |
| **Recover** | Re-run the script. It resumes from the progress file — completed pages are skipped, failed pages are retried. |
| **Cure** | Always run `check_counts.py` after every import and confirm count matches Socrata. This is step 1 of `POST_IMPORT_RUNBOOK.md`. |

---

### Risk 5 — Port 6543 (PgBouncer) breaks COPY with cryptic error

COPY requires a direct Postgres connection (port 5432). PgBouncer (port 6543)
does not support the COPY protocol. Error messages are cryptic and give no hint
the port is the problem (`no COPY in progress`, `unexpected message type`).

| | What to do |
|---|---|
| **Avoid** | The auto-swap line is in all three scripts — never remove it: `DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")` |
| **Mitigate** | Quick test before any import: `python -c "import psycopg2,os; from dotenv import load_dotenv; load_dotenv(); conn=psycopg2.connect(os.getenv('SUPABASE_DB_URL','').replace(':6543/',':5432/')); print('port 5432 OK')"` |
| **Detect** | Error keywords that mean wrong port: `no COPY in progress`, `unexpected message type`, `SSL connection closed unexpectedly`. |
| **Minimise** | Failure happens immediately on the first COPY call — nothing is written, nothing is corrupted. Just fix and restart. |
| **Recover** | Confirm the `.replace(":6543/", ":5432/")` line exists. Re-run the script. |
| **Cure** | Auto-swap line is already in all scripts. Add it to every new import script. |

---

### Risk 6 — COPY used where UPDATE is needed

COPY only does INSERT. Scripts that UPDATE existing rows cannot use COPY. If
mistakenly used, COPY inserts duplicate rows instead of updating — silent data
corruption with no error.

| | What to do |
|---|---|
| **Avoid** | Before writing any script: INSERT into empty table → COPY. UPDATE existing rows → execute_values + port 5432. |
| **Mitigate** | Every script header already declares its operation type in a comment. Read it before modifying. |
| **Detect** | After accidental COPY-instead-of-UPDATE: `SELECT dot_number, COUNT(*) FROM carriers GROUP BY dot_number HAVING COUNT(*) > 1;` — duplicates confirm wrong method. |
| **Minimise** | Run with `--test` flag (pilot mode, no writes) before any full run if unsure of operation type. |
| **Recover** | Delete duplicates: `DELETE FROM table a USING table b WHERE a.id > b.id AND a.dot_number = b.dot_number;` Then re-run correct UPDATE script. |
| **Cure** | UPDATE scripts (`fix_mc_and_fleet.py`, `backfill_inspection_fk-V2.py`) are already labelled. Do not change their flush method to COPY. |

---

### Decision guide — one line

```
Bulk INSERT into truncated table?  →  COPY FROM STDIN, port 5432        ← 4-5x faster
UPDATE existing rows?              →  execute_values, port 5432
UPSERT (insert or update)?         →  execute_values + ON CONFLICT, port 5432
```

### Safe COPY pre-flight checklist

```
[ ] Table is empty or just truncated
[ ] DB_URL auto-swap line present: .replace(":6543/", ":5432/")
[ ] Every field written through _copy_escape()
[ ] SET statement_timeout = 0 before COPY call
[ ] Will run check_counts.py after import to verify row count
[ ] This is an INSERT operation, not an UPDATE
```

---

## 🔴 HIGH PRIORITY STANDING RULE — ALWAYS USE THE COPY METHOD FOR ALL IMPORTS

**Every future import script that does bulk INSERT must use COPY FROM STDIN,
not execute_values. No exceptions. This is 3–4x faster and costs nothing extra.**

When writing or modifying any import script, apply ALL of the following:

### 1. Always use port 5432 (never 6543)

```python
DB_URL = os.getenv("SUPABASE_DB_URL", "").replace(":6543/", ":5432/")
```

Port 6543 is PgBouncer (connection pooler). It does not support COPY.
Port 5432 is direct Postgres. It supports COPY and everything else.
This one line swap is zero risk and always faster.

### 2. Always use COPY FROM STDIN for bulk INSERT

```python
from io import StringIO

def _copy_escape(v):
    if v is None:
        return r"\N"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

def flush_to_db(rows, columns: list, table: str):
    """Standard COPY-based flush. Use this in every reimport script."""
    if not rows:
        return True
    buf = StringIO()
    for row in rows:
        buf.write("\t".join(_copy_escape(v) for v in row) + "\n")
    for attempt in range(4):
        conn = None
        try:
            buf.seek(0)
            conn = psycopg2.connect(DB_URL, connect_timeout=10)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 0")
                    cur.execute("SET synchronous_commit = off")
                    cur.copy_expert(
                        f"COPY {table} ({', '.join(columns)}) FROM STDIN",
                        buf,
                    )
            return True
        except Exception as e:
            log.warning(f"  flush attempt {attempt+1} failed: {e}")
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
        finally:
            try:
                if conn: conn.close()
            except Exception: pass
    log.error("  flush_to_db failed after 4 attempts")
    return False
```

### 3. Always use these batch/flush settings

```python
DL_THREADS  = 20        # parallel Socrata downloads
DL_BATCH    = 10        # pages per download batch
FLUSH_SIZE  = 500_000   # rows per DB flush (fewer round trips)
```

### 4. Do NOT use ON CONFLICT DO NOTHING with COPY

COPY does not support conflict clauses. If the table is truncated before
import (fresh load), there are no conflicts anyway — remove it.

### When COPY does NOT apply

- **UPDATE scripts** (`fix_mc_and_fleet.py`, `backfill_inspection_fk-V2.py`):
  COPY cannot UPDATE existing rows. Use execute_values with port 5432.
- **UPSERT scripts**: COPY to a temp table first, then INSERT ... ON CONFLICT
  DO UPDATE ... FROM temp_table. More complex but still faster than direct upsert.

### Speed reference

| Method | Typical speed | 8M rows |
|---|---|---|
| execute_values, port 6543 | ~700 rows/s | ~3.2 hours |
| execute_values, port 5432 | ~900 rows/s | ~2.5 hours |
| COPY FROM STDIN, port 5432 | ~3,000–5,000 rows/s | ~30–45 min |
| VPS + local Postgres | ~50,000+ rows/s | ~8 min |

**Always use COPY. It is the standard from Jun 22 2026 onward.**

---

## ⚠️ VERY IMPORTANT — DO NOT REPEAT THIS MISTAKE (Jun 2026)

### What happened — in plain English

1. Bugs were found in the inspections table (wrong dates, duplicate rows).
   The fix was to wipe the table and reload fresh data from FMCSA.

2. The wipe command used was `TRUNCATE TABLE inspections CASCADE`.
   **CASCADE doesn't just clear inspections — it also auto-deletes everything
   in the violations table**, because violations has a link (foreign key) back
   to inspections. This link means "every violation row belongs to an inspection
   row." When you wipe the parent (inspections), the database automatically
   wipes all the children (violations) too.

3. Nobody realized this was happening. So every time inspections was "fixed",
   all 13 million violation rows were silently deleted at the same time.

4. On top of that, the computer kept sleeping or shutting down mid-import,
   leaving the tables half-empty. This forced another restart — which ran the
   wipe command again — which deleted violations again. This loop repeated
   3 times over 3 days (Jun 19, 21, 22 2026).

### The fix — already applied to `reimport_inspections_V3.py`

The script has been updated. `TRUNCATE TABLE inspections CASCADE` has been
**removed permanently** and replaced with:

```sql
TRUNCATE TABLE violations;   -- step 1: clear violations explicitly and visibly
TRUNCATE TABLE inspections;  -- step 2: clear inspections, NO CASCADE keyword
```

This way:
- You can see exactly what is being deleted before it happens
- There is no hidden side-effect
- Any future developer reading the script will know both tables are cleared

### Rules going forward

**Rule 1 — NEVER write `TRUNCATE ... CASCADE` in any import script.**
Always truncate each table explicitly and in the right order.

**Rule 2 — Before truncating any table, check what depends on it:**
```sql
SELECT conrelid::regclass AS dependent_table
FROM pg_constraint
WHERE confrelid = 'inspections'::regclass AND contype = 'f';
```
Run this query first. If anything depends on the table you are about to wipe,
truncate those dependent tables first — explicitly, one by one.

**Rule 3 — Never run a 1–3 hour import on a laptop.**
If the machine sleeps or shuts down mid-import after a TRUNCATE, the table is
left empty. Use a server, VPS, or cloud runner that stays on.

**Rule 4 — Do not re-import unless absolutely necessary.**
Re-importing 8M+ rows takes hours and must be sequenced carefully. For row-level
bug fixes, use a targeted `UPDATE` query instead. Only do a full reload when
the FMCSA source data has changed significantly or a schema migration requires it.

### Table dependency map — check this before any TRUNCATE

```
inspections  (8.3M rows)
    └── violations.inspection_id  ←── FK — CASCADE will silently wipe this (7.5M rows)
```

### Status as of Jun 22 2026
The final correct re-import is running now and will be the last one.
After it completes, do not re-import inspections or violations again
unless there is a major FMCSA data change requiring a full reload.

---

## Current task: CFR plain-English violation descriptions
Full details in `../carrier-portal/CLAUDE.md` under "CFR plain-English violation
descriptions". Short version:

- Goal: in `carrier-portal/app/carrier/[dot]/page.tsx` Violations section, add a
  plain-English description next to each violation. Must be REAL FMCSA data,
  not guesses.
- DONE: `violations.violation_code` (1-49, 99) -> plain English, verified
  official FMCSA mapping in `fmcsa_violation_categories.md`. Codes 50-55
  unresolved (not in the official table).
- BLOCKED: `violations.description` (CFR-suffix codes like "395.8K2-HOSRC",
  the "CFR Section" column) has NO mapping yet. Authoritative source (FMCSA
  SMS Appendix A xlsx) returns 403 via curl/WebFetch. See
  `../carrier-portal/CLAUDE.md` for leads already tried.
- Next step: find a working source for Appendix A, or ship the
  `violation_code` category mapping now and leave "CFR Section" as follow-up.

## Other completed work (2026-06-10)
Dedup/reimport of `inspections`, `authority_history`, `insurance` is fully done
and verified — nothing left to do there. See `../carrier-portal/CLAUDE.md` for
record counts.

## Cleanup (low priority)
Leftover temp files: `fmcsa_violations_list.xlsx`, `cookies.txt` (403 error
bodies), and `_verify_*.py` / `_sample_*.py` / `_violation_freq.py` /
`_dedup_cleanup.py` scripts — safe to delete once no longer needed.

---

## FMCSA Dataset Reference — Complete Specification
Source: "Dataset Description and Data Definitions For Select Datasets on DOT's Open Data Catalog"
All datasets found at: https://data.transportation.gov
All datasets update daily by 9:30AM US Eastern Time.

### Dataset Naming Convention
- [Dataset Name] = "Daily Difference" — records updated or added since previous run only. In some cases includes all other records for the same carrier. In some cases includes associated records where update occurred elsewhere but data provided for completeness.
- [Dataset Name] – All With History = "Full/Baseline" — ALL records including historical values as of latest update.

---

### Dataset 1 & 2: "Carrier" or "Carrier – All With History"
Records for all carriers/brokers/freight forwarders with active, inactive, or pending authorities (common or contract). Includes DOT number, docket number, entity census, authority, and insurance data.

Fields:
1. Docket Number — Text 8 — Unique FMCSA number for for-hire motor carriers (MC000000, FF000000 or MX000000)
2. USDOT Number — Text 8 — Official FMCSA registration number for all interstate motor carriers
3. MX Type — Text 1 — X = OP-1 (Operate throughout US); Z = OP-2 (Operate in Commercial Zones only)
4. RFC Number — Text 17 — Mexican Government registration code for Mexican carriers
5. Common Authority — Text 1 — A = Active; I = Inactive; N = No Authority
6. Contract Authority — Text 1 — A = Active; I = Inactive; N = No Authority
7. Broker Authority — Text 1 — A = Active; I = Inactive; N = No Authority
8. Pending Common Authority — Text 1 — Y = Application Pending; N = No Application Pending
9. Pending Contract Authority — Text 1 — Y = Application Pending; N = No Application Pending
10. Pending Broker Authority — Text 1 — Y = Application Pending; N = No Application Pending
11. Common Authority Revocation — Text 1 — Y = In Revocation; N = Not in Revocation
12. Contract Authority Revocation — Text 1 — Y = In Revocation; N = Not in Revocation
13. Broker Authority Revocation — Text 1 — Y = In Revocation; N = Not in Revocation
14. Property — Text 1 — Y/N
15. Passenger — Text 1 — Y/N
16. Household Goods — Text 1 — Y/N
17. Private Check — Text 1 — Y/N
18. Enterprise Check — Text 1 — Y/N
19. BIPD Required — Text 5 — Amount of BI&PD insurance required (in thousands)
20. Cargo Required — Text 1 — Y/N
21. Bond/Surety Required — Text 1 — Y/N
22. BIPD on File — Text 5 — Amount of BI&PD insurance on file (in thousands)
23. Cargo on File — Text 1 — Y/N
24. Bond/Surety on File — Text 1 — Y/N
25. Address Status — Text 1 — Y = Deliverable; N = Undeliverable
26. DBA Name — Text 60 — Doing Business As name
27. Legal Name — Text 120 — Company legal name
Company Business Address:
28. PO Box/Street — Text 50
29. Colonia — Text 30
30. City — Text 30
31. State Code — Text 2
32. Country Code — Text 2
33. Zip Code — Text 10
34. Telephone Number — Text 14 — If on file
35. Fax Number — Text 14 — If on file
Company Mailing Address:
36. PO Box/Street — Text 50
37. Colonia — Text 30
38. City — Text 30
39. State Code — Text 2
40. Country Code — Text 2
41. Zip Code — Text 10
42. Telephone Number — Text 14 — If on file
43. Fax Number — Text 14 — If on file

LITIGATION NOTE: Fields 19 vs 22 (BIPD Required vs BIPD on File) reveal whether carrier was meeting minimum insurance requirements. Fields 5/6/7 authority status and 11/12/13 revocation status are critical for accident date analysis.

---

### Dataset 3 & 4: "Insur" or "Insur – All With History"
Records for carrier/broker/freight forwarder ACTIVE OR PENDING individual insurance policies. Linked to entities by docket number. Multiple records possible per entity.
IMPORTANT: "Insur" daily difference dataset provides insurance policy REMOVALS as "blank" records (other than docket number, all fields show empty or "00000" values).

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. Insurance Type — Text 1 — 1=BI&PD; 2=Cargo; 3=Bond; 4=Trust Fund
3. BI&PD Class — Text 1 — P=Primary; E=Excess; 1=Full Security Limits Under Section 1043.2(b)(1); 2=Full Security Limits Under Section 1043.2(b)(2)
4. BI&PD Maximum Dollar Limit (company shall not be liable for amounts in excess of) — Text 5 — Amount in thousands
5. BI&PD Underlying Dollar Limit — Text 5 — Amount in thousands
6. Policy Number — Text 25 — Insurance policy specific identifier
7. Effective Date — Text 10 — Effective date of the policy
8. Form Code — Text 3 — 34=Cargo; 82=BI&PD; 83=Cargo; 84=Property Broker's Surety Bond; 85=Property Broker's Trust Fund Agreement; 91/91X=BI&PD/BI&PD Primary/BI&PD Excess
9. Insurance Company Name — Text 45 — Note: policy may be administered by a company branch with a different name

NOTE: For Insurance Type 1 (BI&PD), amounts are in fields 4 and 5. For Insurance Types 2, 3, and 4, amounts in fields 4 and 5 will be 0 as they are not BI&PD policies.

---

### Dataset 5 & 6: "ActPendInsur" or "ActPendInsur – All With History"
Information on implementation dates of active or pending insurance policy. Contains posted date, effective date, cancel effective date, insurance company name, BI&PD limits, DOT number and docket number.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
3. Form Code — Text 3 — 34=Cargo; 82=BI&PD; 83=Cargo; 84=Property Broker's Surety Bond; 85=Property Broker's Trust Fund Agreement; 91/91X=BI&PD/BI&PD Primary/BI&PD Excess
4. Insurance Type Description — Text 21 — Description of insurance form/class
5. Insurance Company Name — Text 45 — Note: policy may be administered by a company branch with a different name
6. Policy Number — Text 25 — Insurance policy specific identifier
7. Posted Date — Text 10 — Date FMCSA received the policy
8. BI&PD Underlying Limit — Text 5 — Amount in thousands
9. BI&PD Maximum Limit (company shall not be liable for amounts in excess of) — Text 5 — Amount in thousands
10. Effective Date — Text 10 — Effective date of the policy
11. Cancel Effective Date — Text 10 — Date the policy is effectively cancelled

NOTE: For Form Codes 91, 91X, and 82, insurance amounts are in fields 8 and 9. For Form Codes 34, 83, 84, and 85, amounts in fields 8 and 9 will be 0 as they are not BI&PD policies.

LITIGATION NOTE: Effective Date + Cancel Effective Date = determine if insurance was active on accident date. This is the PRIMARY dataset for the accident date insurance filter.

---

### Dataset 7 & 8: "AuthHist" or "AuthHist – All With History"
Records showing HISTORY of each authority granted to a carrier/broker/freight forwarder. Includes dates of original authority action and final authority action. Multiple records possible per entity.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
3. Sub Number — Text 4 — Action sequence number; not commonly used
4. Operating Authority Type — VARCHAR 128 — Operating Authority Type
5. Original Authority Action Description — Text 60 — Starting authority action (e.g. "granted")
6. Original Authority Action Served Date — Text 10 — Date starting authority action executed
7. Final Authority Action Description — Text 60 — Final authority action (e.g. "revoked")
8. Final Authority Decision Date — Text 10 — Date final authority action determined
9. Final Authority Served Date — Text 10 — Date final authority action became effective

LITIGATION NOTE: Original Authority Action Served Date + Final Authority Served Date = determine if operating authority was valid on accident date. This is the PRIMARY dataset for the accident date authority filter.

---

### Dataset 9 & 10: "BOC3" or "BOC3 – All With History"
Records for each BOC3 agent hired by a carrier/broker/freight forwarder. Each entity MUST hire a BOC3 agent to represent them in legal matters to obtain operating authority. In some cases entities may act as their own BOC3 agent.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
4. Company Name — Text 60 — Process agent company name
5. Attention to or Title — Text 45 — Process agent company contact
6. Street or PO Box — Text 35 — Process agent company address street
7. City — Text 30 — Process agent company address city
8. State — Text 2 — Process agent company address state
9. Country — Text 3 — Process agent company address country
10. Zip Code — Text 10 — Process agent company address zip code

LITIGATION NOTE: BOC3 agent = who to serve legal papers on. Critical for lawyers initiating litigation against a carrier.

---

### Dataset 11 & 12: "InsHist" or "InsHist – All With History"
Contains information on a carrier's PREVIOUS (historical/cancelled) insurance policies. Contains cancellation method, policy type, policy number, effective and cancellation dates.
IMPORTANT NOTE: All insurance information relates to the policy being cancelled, replaced, or prior to a name change. It is NOT the subsequent policy.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
3. Form Code — Text 3 — 34=Cargo; 82=BI&PD; 83=Cargo; 84=Property Broker's Surety Bond; 85=Property Broker's Trust Fund Agreement; 91/91X=BI&PD/BI&PD Primary/BI&PD Excess
4. Cancellation Method — Text 12 — "cancelled" / "replaced" / "name change" / "transferred"
5. Cancel/Replace/Name Change/Transfer Form — Text 6 — Codes for Cancelled: 35=BMC Cancellation Form; 36=BMC Surety Bond Cancellation Form; 85C=BMC Cancellation for Trust Funds. Codes for Replaced: one of the form codes in field 3. Code for Name Change: "NC". Code for Transferred: "TR"
6. Insurance Type Indicator — Text 1 — " " (space) = BIPD; "*" = Not BIPD (Cargo, Surety, or Trust Fund)
7. Insurance Type Description — Text 12 — Description of insurance form/class
8. Policy Number — Text 25 — Insurance policy specific identifier
9. Minimum Coverage Amount — Text 5 — Minimum insurance amount required for the entity in thousands
10. Insurance Class Code — Text 1 — P=Primary; E=Excess
11. Effective Date — Text 10 — Effective date of the insurance policy
12. BI&PD Underlying Limit Amount — Text 10 — Amount in thousands. When Insurance Class Code is "E", underlying limit = value of the primary insurance
13. BI&PD Max Coverage Amount — Text 10 — Maximum dollar amount covered by the policy in thousands
14. Cancel Effective Date — Text 10 — Date the policy is effectively cancelled
15. Specific Cancellation Method — Text 10 — TERM/CANCL = cancellation executed by FMCSA; Term/REPL = replacement executed by new policy submission
17. Insurance Company Branch — Text 2 — Insurance company branch number
18. Insurance Company Name — Text 45 — Insurance company name

NOTE: For Form Codes 91, 91X, and 82, insurance amounts are in fields 12 and 13. For Form Codes 34, 83, 84, and 85, amounts in fields 12 and 13 will be 0 as they are not BI&PD policies.

LITIGATION NOTE: Combined with ActPendInsur, this dataset provides the COMPLETE insurance timeline for a carrier. Effective Date + Cancel Effective Date across both datasets = full picture of insurance coverage on any historical date.

---

### Dataset 13 & 14: "Rejected" or "Rejected – All With History"
Information on insurance forms REJECTED by FMCSA. Contains insurance policy info, date rejected, and reason for rejection. Linked to carrier by DOT number and docket number.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
3. Form Code (Insurance or Cancel) — Text 3 — 34=Cargo; 35=BMC Cancellation Form; 36=BMC Surety Bond Cancellation Form; 82=BI&PD; 83=Cargo; 84=Property Broker's Surety Bond; 85=Property Broker's Trust Fund Agreement; 85C=BMC Cancellation for Trust Funds; 91/91X=BI&PD/BI&PD Primary/BI&PD Excess
4. Insurance Type Description — Text 12 — Insurance type associated with the rejected form
5. Policy Number — Text 25 — Insurance policy specific identifier
6. Received Date — Text 10 — Date FMCSA received the form
7. Insurance Class Code — Text 1 — P=Primary; E=Excess (when available)
8. Insurance Type Code — Text 1 — " " (space) = BI&PD; "*" = Not BI&PD
9. Underlying Limit Amount — Text 10 — Amount in thousands
10. Maximum Coverage Amount — Text 10 — Maximum dollar amount covered by the policy in thousands
11. Rejected Date — Text 10 — Date the submitted form was rejected
13. Insurance Branch — Text 2 — Insurance company branch number
14. Company Name — Text 45 — Insurance company name
15. Rejected Reason — Text 300 — THE REASON THE FORM WAS REJECTED (e.g. "Policy is already cancelled")
16. Minimum Coverage Amount — Text 5 — Minimum insurance amount required for the entity in thousands

LITIGATION NOTE: This is the most litigation-relevant dataset after InsHist. Rejected Reason (field 15, 300 chars) explicitly states why FMCSA rejected an insurance filing. Shows carrier attempted to file insurance and was rejected — powerful evidence of insurance gaps. Display prominently in carrier report.

---

### Dataset 15 & 16: "Revocation" or "Revocation – All With History"
Information on carrier/broker/freight forwarder authorities REVOKED by FMCSA. Includes DOT number, docket number, type of authority revoked, and reason.

Fields:
1. Docket Number — Text 8 — MC000000, FF000000 or MX000000
2. USDOT Number — Text 8 — Official FMCSA registration number
3. Operating Authority Registration Type — VARCHAR 128 — common / contract / broker
4. Serve Date — Text 10 — Date the FIRST revocation letter was sent to the entity
5. Revocation Type — Text 60 — The type of revocation action
6. Effective Date — Text 10 — Date the revocation is effective

LITIGATION NOTE: Serve Date vs Effective Date gap is important — carrier was notified but still operating. If accident occurred between Serve Date and Effective Date, carrier was operating under revocation notice.

---

### FMCSA Content Disclaimer (from source document)
- Each dataset is a SNAPSHOT of data at time generated. Information is constantly changing.
- Data is for informational purposes only and does not constitute a legal contract.
- FMCSA data is not intended as, nor offered as, legal advice.
- FMCSA is not liable for any damage or loss caused by reliance on dataset content.

### Insurance Form Code Master Reference
- 34 = Cargo
- 35 = BMC Cancellation Form
- 36 = BMC Surety Bond Cancellation Form
- 82 = BI&PD
- 83 = Cargo
- 84 = Property Broker's Surety Bond
- 85 = Property Broker's Trust Fund Agreement
- 85C = BMC Cancellation for Trust Funds
- 91 = BI&PD
- 91X = BI&PD/Primary or BI&PD/Excess

### Accident Date Filter Logic (using these datasets)
To answer "what was this carrier's status on [accident date]":

INSURANCE: Query ActPendInsur where Effective Date <= accident date AND (Cancel Effective Date >= accident date OR Cancel Effective Date is null). Cross-reference InsHist for cancelled policies that covered that date.

AUTHORITY: Query AuthHist where Original Authority Action Served Date <= accident date AND (Final Authority Served Date >= accident date OR Final Authority Served Date is null).

REVOCATION: Query Revocation where Serve Date <= accident date (carrier was notified) or Effective Date <= accident date (revocation was in effect).

INSURANCE GAPS: Query Rejected where Received Date is near accident date — shows carrier attempted insurance filing that was rejected around time of accident.
