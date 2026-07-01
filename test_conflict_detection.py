"""
test_conflict_detection.py
==========================
Synthetic tests for the conflict-report layer (Python side).
No DB connection needed — CarrierFacts objects are built directly.

Run:
    python test_conflict_detection.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from carrier_facts import (
    CarrierFacts,
    NOT_FOUND, CONFIRMED_ACTIVE, NOT_REQUIRED,
    FOR_HIRE_INTERSTATE, PRIVATE,
)
from validation_rules import run_validation_with_conflicts

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = "PASS"
FAIL = "FAIL"

failures = 0


def assert_true(condition, label):
    global failures
    icon = PASS if condition else FAIL
    print(f"  [{icon}]  {label}")
    if not condition:
        failures += 1


def base_facts(**overrides):
    """Minimal valid CarrierFacts for a private carrier. Override to trigger rule failures."""
    defaults = dict(
        dot_number="TEST000",
        mc_number=None,
        legal_name="TEST CARRIER LLC",
        carrier_type=PRIVATE,
        usdot_status="ACTIVE",
        authority_required="NO",
        authority_status=NOT_REQUIRED,
        authority_revocation_date=None,
        first_authority_date=None,
        active_authority_period=None,
        insurance_required="NO",
        insurance_status=NOT_REQUIRED,
        insurance_cancellation=NOT_REQUIRED,
        insurance_replacement=NOT_REQUIRED,
        fleet_power_units=2,
        fleet_drivers=2,
        inspection_count=5,
        crash_count=0,
        violation_count=0,
        boc3_on_file="NO",
        sms_percentile_present=False,
        accident_date=None,
        data_confidence={"authority": "HIGH", "insurance": "HIGH", "fleet": "HIGH", "identity": "HIGH"},
    )
    defaults.update(overrides)
    return CarrierFacts(**defaults)


# ─── Test 1: fleet asymmetry ──────────────────────────────────────────────────

print("\nTest 1: fleet_data_not_asymmetric — power_units=5, drivers=0")

facts1 = base_facts(fleet_power_units=5, fleet_drivers=0)
results1, conflicts1 = run_validation_with_conflicts(facts1)

rule_fired = any(r.rule_name == "fleet_data_not_asymmetric" and not r.passed for r in results1)
assert_true(rule_fired, "rule fleet_data_not_asymmetric fires (passed=False)")
assert_true(len(conflicts1) > 0, "conflict report is non-empty")
fleet_c = next((c for c in conflicts1 if c["rule"] == "fleet_data_not_asymmetric"), None)
assert_true(fleet_c is not None, "fleet conflict entry present")
assert_true(fleet_c is not None and "fleet_power_units" in fleet_c["fields"], "fields list includes fleet_power_units")
assert_true(fleet_c is not None and bool(fleet_c["detail"]), "detail string is non-empty")
assert_true(len(facts1.validation_conflicts) > 0, "facts.validation_conflicts populated in-place")
assert_true(isinstance(results1, list) and len(results1) == 20, "returns 20 rule results (no crash)")


# ─── Test 2: insurance date inversion ────────────────────────────────────────

print("\nTest 2: insurance_dates_internally_consistent — cancel before effective")

bad_ins = {
    "effective_date":    "2024-06-01",
    "cancellation_date": "2024-01-01",   # impossible: cancelled before policy started
    "policy_type":       "91",
    "insurer_name":      "Test Insurer",
    "policy_number":     "POL-001",
}
facts2 = base_facts(
    carrier_type=FOR_HIRE_INTERSTATE,
    authority_required="YES",
    authority_status=CONFIRMED_ACTIVE,
    insurance_required="YES",
    insurance_status=CONFIRMED_ACTIVE,
    insurance_cancellation="NO",
    insurance_replacement="NO",
    mc_number="MC123456",
    _insurance_records=[bad_ins],
)
results2, conflicts2 = run_validation_with_conflicts(facts2)

ins_fired = any(r.rule_name == "insurance_dates_internally_consistent" and not r.passed for r in results2)
assert_true(ins_fired, "rule insurance_dates_internally_consistent fires (passed=False)")
assert_true(len(conflicts2) > 0, "conflict report is non-empty")
ins_c = next((c for c in conflicts2 if c["rule"] == "insurance_dates_internally_consistent"), None)
assert_true(ins_c is not None, "insurance date conflict entry present")
assert_true(ins_c is not None and bool(ins_c["detail"]), "detail string is non-empty")
assert_true(len(facts2.validation_conflicts) > 0, "facts.validation_conflicts populated in-place")
assert_true(isinstance(results2, list) and len(results2) == 20, "returns 20 rule results (no crash)")


# ─── Test 3: clean carrier — no conflicts ─────────────────────────────────────

print("\nTest 3: clean carrier produces empty conflict list")

facts3 = base_facts()
results3, conflicts3 = run_validation_with_conflicts(facts3)

assert_true(len(conflicts3) == 0, "conflict list is empty")
assert_true(len(facts3.validation_conflicts) == 0, "facts.validation_conflicts is empty")
assert_true(isinstance(results3, list) and len(results3) == 20, "returns 20 rule results")


# ─── Test 4: reinstated carrier — no rule 01 conflict ────────────────────────
# Mirrors DOT 830598 / 833248 / 1074419 pattern: CONFIRMED_ACTIVE carrier
# that has had historical involuntary revocations, all subsequently reversed.
# authority_revocation_date must be None (cleared by _infer_authority_status
# for CONFIRMED_ACTIVE), so rule 01 must pass.

print("\nTest 4: CONFIRMED_ACTIVE + revocation_date=None (reinstatement pattern, rule 01 must PASS)")

facts4 = base_facts(
    carrier_type=FOR_HIRE_INTERSTATE,
    authority_required="YES",
    authority_status=CONFIRMED_ACTIVE,
    authority_revocation_date=None,   # by design: cleared for CONFIRMED_ACTIVE
    mc_number="MC368211",
    insurance_required="YES",
    insurance_status=CONFIRMED_ACTIVE,
    insurance_cancellation="NO",
    insurance_replacement="NO",
)
results4, conflicts4 = run_validation_with_conflicts(facts4)

rule_01 = next((r for r in results4 if r.rule_name == "authority_not_both_active_and_revoked"), None)
assert_true(rule_01 is not None, "rule 01 present in results")
assert_true(rule_01 is not None and rule_01.passed, "rule 01 PASSES for CONFIRMED_ACTIVE + None revocation_date")
assert_true(not any(c["rule"] == "authority_not_both_active_and_revoked" for c in conflicts4),
            "no authority_not_both_active_and_revoked conflict in conflict report")
assert_true(isinstance(results4, list) and len(results4) == 20, "returns 20 rule results (no crash)")

# ─── Summary ─────────────────────────────────────────────────────────────────

print(f"\n{'─' * 60}")
if failures == 0:
    print("All tests PASSED.")
else:
    print(f"{failures} test(s) FAILED.")
    sys.exit(1)
