# Post-Import Runbook — Run Once All Imports Complete

Follow these steps IN ORDER after all 3 import windows show "DONE".

---

## Step 1 — Verify counts (run immediately after violations window closes)

```powershell
cd "c:\Users\chong\OneDrive\Documents\Desktop\MISC PROJECT\US DIRECTORY\CARRIER INTELLIGENT REPORT\5 Jun 26 - CARRIER PORTAL\CODES"
python check_counts.py
```

Expected minimums:
- inspections: ≥ 8,200,000
- violations:  ≥ 7,000,000
- insurance:   ≥ 7,200,000  ✅ already at 7,296,997

If inspections or violations are below these numbers, do NOT proceed. Re-run
the appropriate import script (it will resume from where it left off).

---

## Step 2 — Run FK backfill (links violations → inspections)

```powershell
python backfill_inspection_fk-V2.py
```

This links `violations.inspection_id` to `inspections.id` so the portal
can display which inspection each violation belongs to, with the correct date.

Expected output: "Fill rate: XX%" — anything above 30% is acceptable.
Carriers with multiple inspections will be skipped (ambiguous match).

This takes ~15–30 min. Resumable if interrupted.

---

## Step 3 — Verify counts again

```powershell
python check_counts.py
```

Confirm the same minimums as Step 1 — backfill should not change row counts,
only update the inspection_id column.

---

## Step 4 — Re-enable computer sleep

Sleep was disabled for the import run. Re-enable it now:

```powershell
powercfg /change standby-timeout-ac 20
```

---

## Step 5 — Spot-check two known carriers in the portal

Open the portal and check these two carriers:

| DOT    | Carrier                     | What to verify                                      |
|--------|-----------------------------|-----------------------------------------------------|
| 204814 | BINKS COCA COLA BOTTLING CO | Drivers: 13, Trucks: 14 ✅ (already confirmed in DB) |
| 2259497| BUCKSHOT TRANSPORTATION INC | Drivers: 2, Trucks: 2, active insurance should show  |

---

## Step 6 — Run the authority/insurance validation report

```powershell
python validate_authority_insurance.py
```

Output is also saved to `validate_authority_insurance_report.txt`.

Open FMCSA SAFER (https://safer.fmcsa.dot.gov/) and manually verify
~5 carriers from each section:
- Active authority
- Revoked authority
- Reinstated authority
- Cancelled insurance
- Replaced insurance

Target: near-100% agreement. If mismatches found, investigate the
`deriveAuthorityBasis` / `deriveInsuranceBasis` logic in CarrierDetailView.tsx.

---

## Step 7 — Run the full accuracy audit (optional, takes ~10 min)

```powershell
python verify_accuracy_20-V1.py
```

Compares 20 random carriers in DB against Socrata live data.
Checks: name, status, crash count, inspection count, insurance count.

---

## What NOT to do after this point

- Do NOT re-run `reimport_inspections_V3.py` unless FMCSA data has changed
  significantly. It will truncate violations and inspections and start from zero.
  See CLAUDE.md "VERY IMPORTANT" section.
- Do NOT run any script with `TRUNCATE ... CASCADE` on inspections.
- Do NOT run multiple heavy-write scripts in parallel (causes 5× slowdown).
