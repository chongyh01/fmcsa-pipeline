# Git Repository Structure — Carrier Check USA

## Two separate repos, do not merge

| Repo | URL | Branch | What it tracks |
|---|---|---|---|
| carrier-portal | https://github.com/chongyh01/carrier-portal | main | Next.js UI (CarrierDetailView, page, KeyFindings, etc.) |
| fmcsa-pipeline | https://github.com/chongyh01/fmcsa-pipeline | master | Python pipeline (carrier_facts, validation_rules, reimport scripts, etc.) |

## Local paths

| Repo | Local path |
|---|---|
| carrier-portal | `5 Jun 26 - CARRIER PORTAL/carrier-portal/` |
| fmcsa-pipeline | `5 Jun 26 - CARRIER PORTAL/CODES/` |

## Commit discipline

- Any Python pipeline change → commit and push to `fmcsa-pipeline`
- Any UI change → commit and push to `carrier-portal`
- After each verified fix: commit immediately with a specific message
- After any milestone: create a git tag and push it
- Never force-push over existing history without explicit confirmation

## Current milestone tags

| Tag | Repo | What it represents |
|---|---|---|
| v1.1-audit-fixes | carrier-portal | Bug 1 carrier-type fix, Bug 2 authority reinstatement fix, Bugs 3-5 wording, Task F conflict detection. Regression 12/12, audit 8/8 passing. |

## Initial commit history — fmcsa-pipeline (as of Jul 1 2026)

| Commit | Message |
|---|---|
| f0d7842 | feat: Task F + Bug 1/2 fixes — carrier_facts.py, validation_rules.py, gold_carriers.json |
| a1c2408 | fix: Bugs 3-5 wording in audit batch report template + 30-carrier batch script |
| c9122a6 | chore: baseline commit — all CODES pipeline scripts (initial git tracking) |

## Initial commit history — carrier-portal (recent, as of Jul 1 2026)

| Commit | Message |
|---|---|
| f97fae5 | fix: violation date accuracy — FK embed with fallback + 24-month window filter |
| 28b29eb | feat: Task F — data conflict detection layer (DataConflictBanner, ConflictFlag) |
| 16d4b43 | feat: complete remaining UI improvements — validation checker, PDF export, citations, pattern analysis |
