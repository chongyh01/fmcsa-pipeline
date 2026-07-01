# REPORT_PIPELINE_REBUILD.md

**Status:** Active spec — Phase 0 (regression) + Phase 1 (pipeline)
**Owner:** YH
**For:** Claude Code
**Date captured:** 26 Jun 2026

---

## Decision (final)

**Do NOT start from scratch. Do NOT narrow to 3–5 states.**

The 2-week import (8.29M inspections, 13.2M violations, 2.31M crashes, 7.22M insurance) is the correct foundation and stays. The audit proved every critical bug is in the **report layer** — interpretation, joins, date logic, wording — not in the data.

Confidence today: identity/fleet 75–80%, authority/insurance interpretation 60–65%. The gap is logic, not corrupt rows.

**Fix = rebuild report generation as three separate layers.**

---

## Phase 0 — Feature freeze + regression suite (DO THIS FIRST)

Before any functional change, stop reactive bug-chasing. The current loop — fix a bug, find a new one, fix it, break the old one — eats months. The fix is a safety net that proves each change preserves or improves correctness.

**Validation vs regression — two different jobs, both needed:**
- **Validation engine** (Layer 2) — catches contradictions *inside one report* (active+revoked, private+insurance-required). "Is this report internally consistent?"
- **Regression suite** (Phase 0) — catches *breakage across changes*: did this week's fix silently change a carrier that was already correct? "Did I change an answer that was right?"

### Feature freeze

Add to CLAUDE_MASTER.md and enforce until the regression suite is green:

> **FEATURE FREEZE ACTIVE.** No new features until the regression suite passes. Frozen: lawyer dashboard, PACER, AI summaries, related companies, distress alerts, Stripe, any new portal feature. Allowed work: Phase 0 (regression), Phase 1 (facts + validation), and fixes to the ~10 known defect categories only.

### The ~10 defect categories (the whole problem is bounded)

Almost every bug found belongs to one of these. Not 500 — about 10. If no *new* categories appear after these are fixed, the project is converging, not circling.

1. Date parsing
2. Authority interpretation
3. Insurance interpretation
4. Private vs for-hire logic
5. Status wording
6. Duration calculations
7. Time filtering (24-month windows)
8. Contradictory summaries
9. Missing data interpreted as zero
10. Narrative wording

### Regression deliverables

**`gold_carriers.json`** — 100–200 curated DOTs with locked expected answers. Must spread across: active, revoked, for-hire interstate, private, intrastate, passenger, new authority, old authority, single-truck, large fleet. Seed it with the 8 audit DOTs. Each entry stores known-correct facts:

```json
{
  "204814": {
    "legal_name": "BINKS COCA COLA BOTTLING CO",
    "carrier_type": "PRIVATE",
    "usdot_status": "ACTIVE",
    "authority_required": "NO",
    "authority_status": "NOT_REQUIRED",
    "insurance_required": "NO",
    "fleet_power_units": 14,
    "fleet_drivers": 13
  }
}
```

(Expected values verified manually against SAFER before locking. A wrong expected value is worse than none.)

**`run_regression.py`** — rebuilds CARRIER_FACTS for every gold carrier, diffs each field against expected, prints `N identical / N changed`, exits non-zero on any unexpected change. Run after every code change. Any diff = blocked until investigated (could be a regression, or an intended fix that needs the expected value updated).

```
100 carriers tested
98 identical
 2 changed → investigate before release
```

This replaces "I think I fixed it" with objective evidence.

---

## Architecture

```
FMCSA data (keep all, nationwide)
   → Layer 1: CARRIER_FACTS object   (facts only, NO prose, NO conclusions)
   → Layer 2: Validation engine      (rules reject impossible combinations)
   → Layer 3: Confidence gate        (beta = high-confidence carrier types only)
   → Layer 4: Narrative              (templated, litigation-safe language)
```

### Hard rule for Claude Code

> **Facts first, validation second, narrative third.**
> The report generator must NEVER infer facts directly from raw SQL joins or from missing records. It generates ONLY from a CARRIER_FACTS object that has already passed validation.

### The single most important rule

> The system must NEVER convert "no record found" into "no insurance", "no authority", or "no issue found" — unless source logic explicitly proves that conclusion (e.g. cancellation filing present AND no replacement filing).

---

## Layer 1 — CARRIER_FACTS object

Build a function that returns a structured facts object per DOT. No narrative, no judgment words. Every field is either a known value or an explicit `NOT_FOUND` / `REQUIRES_VERIFICATION`.

```
CARRIER_FACTS {
  dot_number
  mc_number                  // NULL if none — never render "MC #MC"
  legal_name
  carrier_type               // FOR_HIRE_INTERSTATE | PRIVATE | INTRASTATE | PASSENGER | UNKNOWN
  usdot_status               // ACTIVE | INACTIVE | NOT_FOUND      (separate from authority)
  authority_required         // YES | NO        (NO for private/intrastate)
  authority_status           // CONFIRMED_ACTIVE | CONFIRMED_REVOKED | NOT_REQUIRED | NOT_FOUND | REQUIRES_VERIFICATION
  authority_revocation_date  // date | NULL
  first_authority_date       // date | NULL
  active_authority_period    // computed span of ACTIVE authority, not "first seen to now"
  insurance_required         // YES | NO
  insurance_status           // CONFIRMED_ACTIVE | CONFIRMED_LAPSED | NOT_REQUIRED | NOT_FOUND | REQUIRES_VERIFICATION
  insurance_cancellation     // YES | NO | NOT_FOUND
  insurance_replacement      // YES | NO | NOT_FOUND
  fleet_power_units          // int
  fleet_drivers              // int
  inspection_count           // int (deduped)
  crash_count                // int
  violation_count            // int
  boc3_on_file               // YES | NO | NOT_FOUND
  sms_percentile_present     // bool
  accident_date              // the date filter the report is built around
  data_confidence            // per-conclusion: HIGH | MEDIUM | LOW
}
```

Rules for Layer 1:
- Separate `usdot_status` from `authority_status`. They are different things.
- Separate `carrier_type` logic: private/intrastate → `authority_required = NO`, `insurance_required = NO` (FMCSA BI&PD public filing generally not required; private insurance may exist outside FMCSA).
- `active_authority_period` = actual active span. If authority existed 2004–2008, do NOT say "operating 21 years". Compute the real active window.
- Dedup inspection rows BEFORE counting (current duplicate rows inflate OOS).
- Dates: parse globally, store one canonical format. Never let `01-05-2026`, `05/01/2026`, `05 Jan 2026` disagree inside one report.

---

## Layer 2 — Validation engine

Report does NOT generate unless ALL rules pass. Each failed rule = blocking ERROR logged with DOT + rule id.

Start with these ~20 rules (drawn directly from the audit). Add more as new contradictions surface; target grows toward ~200 over time.

| # | Rule | Fail condition |
|---|------|----------------|
| 1 | authority active + revoked | `authority_status=CONFIRMED_ACTIVE` AND `authority_revocation_date` present |
| 2 | private + authority required | `carrier_type=PRIVATE` AND `authority_required=YES` |
| 3 | private + insurance required | `carrier_type=PRIVATE` AND `insurance_required=YES` |
| 4 | intrastate + "NOT AUTHORIZED" | `carrier_type=INTRASTATE` AND authority rendered as NOT AUTHORIZED |
| 5 | inspections=0 + SMS percentile | `inspection_count=0` AND `sms_percentile_present=true` |
| 6 | no record → no insurance | `insurance_status=CONFIRMED_LAPSED` AND (`insurance_cancellation≠YES` OR `insurance_replacement=NOT_FOUND` not proven) |
| 7 | no record → no authority | `authority_status=CONFIRMED_REVOKED` from absence of records rather than a revocation event |
| 8 | summary vs findings authority mismatch | authority_status differs between summary block and body |
| 9 | summary vs findings active/inactive | usdot_status active/inactive differs across sections |
| 10 | MC prefix bug | rendered MC number contains literal "MC" with null mc_number |
| 11 | duration math (insurance) | stated lapse years ≠ computed years (±1 tolerance) |
| 12 | duration math (operating) | "operating X years" ≠ `active_authority_period` |
| 13 | old event in 24-month section | event date older than 24 months rendered inside a "within 24 months" block |
| 14 | "No issues found" wording | any output contains "No Authority Issues Found" / "No issues found" |
| 15 | overconfident absence | any conclusion asserts a negative from NOT_FOUND without proof |
| 16 | fleet mismatch flag | power_units or drivers deviate from last known SAFER value (flag, MEDIUM confidence) |
| 17 | inspection count source | inspection_count taken from un-deduped table |
| 18 | missing data_confidence | any authority/insurance conclusion lacks a confidence value |
| 19 | for-hire classification | carrier rendered "authorized for-hire" when source suggests service/intrastate |
| 20 | accident_date missing | report built without a resolved accident_date filter |

---

## Layer 3 — Confidence gate (beta scope)

Keep the full nationwide DB and pipeline. Gate **output**, not data.

**Beta releases reports ONLY for:** `carrier_type = FOR_HIRE_INTERSTATE` with `authority_status` and `insurance_status` both `CONFIRMED_*` and `data_confidence = HIGH`.

Everything else (PRIVATE, INTRASTATE, UNKNOWN, any NOT_FOUND / REQUIRES_VERIFICATION) → withhold from beta, surface as "Requires Verification". This gives lawyer feedback on the strongest cases while edge-case logic matures.

---

## Layer 4 — Narrative

Only runs on a validated CARRIER_FACTS object. Templated, no freeform legal conclusions.

Reuse existing litigation-safe rules:
- Prohibited: "negligent", "liable", "at fault", "negligence per se".
- Required framing: "consistent with", "warrants verification of".
- No risk verdicts, no star ratings, no emoji.
- Replace "No Authority Issues Found" with exact status: **Confirmed Active / Confirmed Revoked / Not Required / Not Found / Requires Verification**.
- Show `data_confidence` beside every authority and insurance conclusion.

---

## Audit verification targets (re-check after rebuild)

| DOT | Carrier | Expected / flag |
|---|---|---|
| 204814 | BINKS COCA COLA BOTTLING | 14 trucks / 13 drivers — confirm still correct |
| 3431540 | YAFET TRUCKING | inspection count: report said 12, SMS shows ~9 — verify dedup |
| 3612954 | AMPF LOGISTICS | active vs revoked + no insurance contradiction — must pass rules 1,6,7 |
| 3841767 | ABLE BODY LOGISTICS | summary inactive vs findings active — must pass rules 8,9 |
| 623336 | CARBARB ENTERPRISES | revoked vs active conflict — manual SAFER verify |
| 3012101 | XIANGFENG TRADING | private, 1/1 — confirm private logic rules 2,3 |
| 1612145 | J8 EQUIPMENT | private property 1/1 — confirm |
| 2308088 | JEREMIAH BROOKS | "for-hire" wording vs service/intrastate — rule 19 |

---

## Beta gate criteria (don't ship until)

1. Layer 1 + Layer 2 built nationwide.
2. Random audit of **100–200 carriers** across types (for-hire, private, passenger, intrastate, interstate, revoked, active, new/old authority, single-truck, large fleet) vs SAFER.
3. **99% factual accuracy** on the Carrier Facts Sheet.
4. Zero validation rule failures on the beta-gated (high-confidence for-hire) set.

---

## Instruction to Claude Code

> Read CLAUDE_MASTER.md and PROJECT_INTENT.md first. Then implement REPORT_PIPELINE_REBUILD.md.
>
> **FEATURE FREEZE is active** — no new features until the regression suite is green. Build in this order:
>
> **Phase 0 first:** `gold_carriers.json` (seed with the 8 audit DOTs, expand toward 100–200 across carrier types, expected values verified vs SAFER) and `run_regression.py` (rebuild facts for all gold carriers, diff vs expected, exit non-zero on any unexpected change). Run it after every change from now on.
>
> **Then Phase 1:** STOP generating reports from raw SQL joins. Build the CARRIER_FACTS object (Layer 1) and the validation engine (Layer 2) before touching narrative. Every report must be generated from a CARRIER_FACTS object that has passed all validation rules. Never convert "no record found" into "no insurance / no authority / no issue". Facts first, validation second, narrative third.
>
> Phase 1 deliverables: (1) `carrier_facts.py` builder, (2) `validation_rules.py` with rules 1–20, (3) `audit_8_dots.py` to run both over the 8 audit DOTs and print pass/fail.
>
> Do not modify import or dedup scripts. Do not TRUNCATE CASCADE. Do not add frozen features.
