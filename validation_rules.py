"""
validation_rules.py
===================
Layer 2 of the report pipeline: 20 validation rules for CarrierFacts.

Usage:
    from validation_rules import run_validation
    results = run_validation(facts)
    failed = [r for r in results if not r.passed]

Each rule function takes a CarrierFacts and returns a RuleResult.
All 20 rules always run (no short-circuit) so you get the full picture.
Report generation must halt if any rule returns passed=False.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RuleResult:
    rule_id:   int
    rule_name: str
    passed:    bool
    message:   str

    def __str__(self):
        icon = "PASS" if self.passed else "FAIL"
        return f"[{icon}] Rule {self.rule_id:02d} {self.rule_name}: {self.message}"


def _ok(rule_id, name, note="") -> RuleResult:
    return RuleResult(rule_id, name, True,  f"OK{' — ' + note if note else ''}")

def _fail(rule_id, name, msg) -> RuleResult:
    return RuleResult(rule_id, name, False, msg)

def _na(rule_id, name, reason) -> RuleResult:
    return RuleResult(rule_id, name, True,  f"N/A — {reason}")


# ── Date helper ───────────────────────────────────────────────────────────────

def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, date):
        return val
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(val)[:10], fmt).date()
        except ValueError:
            continue
    return None


# ── Import sentinels lazily to avoid circular import issues ───────────────────

def _cf():
    import carrier_facts as cf
    return cf


# ══════════════════════════════════════════════════════════════════════════════
# THE 20 RULES
# ══════════════════════════════════════════════════════════════════════════════

def rule_01_authority_not_both_active_and_revoked(f) -> RuleResult:
    """
    Code-integrity guard: if authority_status=CONFIRMED_ACTIVE,
    authority_revocation_date must be None.

    By design, _infer_authority_status() only sets authority_revocation_date
    for CONFIRMED_REVOKED carriers.  For CONFIRMED_ACTIVE carriers that have
    a historical revocation reversed by a subsequent reinstatement or new grant
    (e.g. carriers with many annual revocation/reinstatement cycles), the
    revocation_date field is cleared — historical events stay in _authority_records.
    This rule fires only if the inference logic has a bug setting both fields
    simultaneously.  A carrier with a past revocation and a current active
    status is a normal reinstatement pattern, NOT a data conflict.
    """
    cf = _cf()
    if f.authority_status != cf.CONFIRMED_ACTIVE:
        return _na(1, "authority_not_both_active_and_revoked", "authority is not CONFIRMED_ACTIVE")
    if f.authority_revocation_date is not None:
        return _fail(1, "authority_not_both_active_and_revoked",
                     f"authority_status=CONFIRMED_ACTIVE but authority_revocation_date="
                     f"{f.authority_revocation_date} — inference logic error: revocation_date "
                     f"must be None for active carriers")
    return _ok(1, "authority_not_both_active_and_revoked")


def rule_02_private_no_authority_required(f) -> RuleResult:
    """
    PRIVATE carriers do not need FMCSA operating authority.
    authority_required must not be YES for a PRIVATE carrier.
    """
    cf = _cf()
    if f.carrier_type != cf.PRIVATE:
        return _na(2, "private_no_authority_required", f"carrier_type={f.carrier_type}")
    if f.authority_required == "YES":
        return _fail(2, "private_no_authority_required",
                     "carrier_type=PRIVATE but authority_required=YES")
    return _ok(2, "private_no_authority_required", f"authority_required={f.authority_required}")


def rule_03_private_no_insurance_required(f) -> RuleResult:
    """
    PRIVATE carriers do not file public BI&PD insurance with FMCSA.
    insurance_required must not be YES for a PRIVATE carrier.
    """
    cf = _cf()
    if f.carrier_type != cf.PRIVATE:
        return _na(3, "private_no_insurance_required", f"carrier_type={f.carrier_type}")
    if f.insurance_required == "YES":
        return _fail(3, "private_no_insurance_required",
                     "carrier_type=PRIVATE but insurance_required=YES")
    return _ok(3, "private_no_insurance_required", f"insurance_required={f.insurance_required}")


def rule_04_intrastate_not_rendered_as_revoked(f) -> RuleResult:
    """
    INTRASTATE carriers never had FMCSA authority in the first place.
    Their authority_status must be NOT_REQUIRED (not CONFIRMED_REVOKED,
    which would falsely imply they had and lost federal authority).
    """
    cf = _cf()
    if f.carrier_type != cf.INTRASTATE:
        return _na(4, "intrastate_not_rendered_as_revoked", f"carrier_type={f.carrier_type}")
    bad_statuses = {cf.CONFIRMED_REVOKED, cf.CONFIRMED_ACTIVE}
    if f.authority_status in bad_statuses:
        return _fail(4, "intrastate_not_rendered_as_revoked",
                     f"carrier_type=INTRASTATE but authority_status={f.authority_status} "
                     f"(should be NOT_REQUIRED)")
    return _ok(4, "intrastate_not_rendered_as_revoked",
               f"authority_status={f.authority_status}")


def rule_05_no_sms_without_inspections(f) -> RuleResult:
    """
    SMS percentile scores require inspection data. A carrier with zero
    inspections cannot have SMS percentiles — they would be meaningless.
    """
    if f.inspection_count > 0:
        return _na(5, "no_sms_without_inspections",
                   f"inspection_count={f.inspection_count}")
    if f.sms_percentile_present:
        return _fail(5, "no_sms_without_inspections",
                     "inspection_count=0 but sms_percentile_present=True — "
                     "SMS percentiles cannot exist without inspections")
    return _ok(5, "no_sms_without_inspections", "inspection_count=0, no SMS — consistent")


def rule_06_lapsed_requires_explicit_cancellation(f) -> RuleResult:
    """
    CONFIRMED_LAPSED requires explicit proof of cancellation (insurance_cancellation=YES).
    Absence of insurance records alone is NOT sufficient to conclude lapsed.
    """
    cf = _cf()
    if f.insurance_status != cf.CONFIRMED_LAPSED:
        return _na(6, "lapsed_requires_explicit_cancellation",
                   f"insurance_status={f.insurance_status}")
    if f.insurance_cancellation != "YES":
        return _fail(6, "lapsed_requires_explicit_cancellation",
                     f"insurance_status=CONFIRMED_LAPSED but insurance_cancellation="
                     f"{f.insurance_cancellation} — no explicit cancellation proof")
    return _ok(6, "lapsed_requires_explicit_cancellation",
               "cancellation confirmed by explicit record")


def rule_07_revoked_requires_revocation_event(f) -> RuleResult:
    """
    CONFIRMED_REVOKED requires an actual revocation event with a date.
    Absence of authority records alone must not produce CONFIRMED_REVOKED.
    """
    cf = _cf()
    if f.authority_status != cf.CONFIRMED_REVOKED:
        return _na(7, "revoked_requires_revocation_event",
                   f"authority_status={f.authority_status}")
    if f.authority_revocation_date is None:
        return _fail(7, "revoked_requires_revocation_event",
                     "authority_status=CONFIRMED_REVOKED but no revocation_date — "
                     "inferred from absence, not from an actual revocation event")
    return _ok(7, "revoked_requires_revocation_event",
               f"revocation_date={f.authority_revocation_date}")


def rule_08_authority_status_is_valid_sentinel(f) -> RuleResult:
    """
    authority_status must be one of the five defined sentinels.
    Any other value indicates a builder bug.
    """
    cf = _cf()
    valid = {
        cf.CONFIRMED_ACTIVE, cf.CONFIRMED_REVOKED, cf.NOT_REQUIRED,
        cf.NOT_FOUND, cf.REQUIRES_VERIFICATION,
    }
    if f.authority_status not in valid:
        return _fail(8, "authority_status_is_valid_sentinel",
                     f"authority_status={f.authority_status!r} is not a defined sentinel")
    return _ok(8, "authority_status_is_valid_sentinel", f"={f.authority_status}")


def rule_09_usdot_status_is_valid(f) -> RuleResult:
    """
    usdot_status must be one of the known FMCSA carrier statuses or NOT_FOUND.
    """
    cf = _cf()
    valid = {"ACTIVE", "INACTIVE", "NOT AUTHORIZED", "OUT-OF-SERVICE", cf.NOT_FOUND}
    if f.usdot_status not in valid:
        return _fail(9, "usdot_status_is_valid",
                     f"usdot_status={f.usdot_status!r} is not a recognised value")
    return _ok(9, "usdot_status_is_valid", f"={f.usdot_status}")


def rule_10_no_mc_placeholder(f) -> RuleResult:
    """
    mc_number must not be the literal string 'MC' (the import placeholder
    for carriers where the docket number was missing). It must be NULL
    or a valid formatted number like 'MC000074'.
    """
    if f.mc_number == "MC":
        return _fail(10, "no_mc_placeholder",
                     "mc_number='MC' — placeholder not resolved to NULL; "
                     "this carrier has no real MC number on file")
    return _ok(10, "no_mc_placeholder",
               f"mc_number={f.mc_number or '(none)'}")


def rule_11_insurance_dates_internally_consistent(f) -> RuleResult:
    """
    For any insurance record, cancellation_date must be AFTER effective_date.
    A policy cancelled before it started is impossible and indicates bad data.
    """
    cf = _cf()
    if f.insurance_status in (cf.NOT_FOUND, cf.NOT_REQUIRED):
        return _na(11, "insurance_dates_internally_consistent",
                   f"insurance_status={f.insurance_status}")
    for rec in f._insurance_records:
        eff    = _parse_date(rec.get("effective_date"))
        cancel = _parse_date(rec.get("cancellation_date"))
        if eff and cancel and cancel < eff:
            return _fail(11, "insurance_dates_internally_consistent",
                         f"cancellation_date {cancel} < effective_date {eff} for "
                         f"{rec.get('insurer_name', 'unknown insurer')} — impossible")
    return _ok(11, "insurance_dates_internally_consistent")


def rule_12_first_authority_date_not_future(f) -> RuleResult:
    """
    first_authority_date must be in the past (or today).
    A future grant date indicates a data import error.
    """
    if f.first_authority_date is None:
        return _na(12, "first_authority_date_not_future", "no first_authority_date")
    d = _parse_date(f.first_authority_date)
    if d is None:
        return _fail(12, "first_authority_date_not_future",
                     f"first_authority_date={f.first_authority_date!r} could not be parsed")
    if d > date.today():
        return _fail(12, "first_authority_date_not_future",
                     f"first_authority_date={f.first_authority_date} is in the future")
    return _ok(12, "first_authority_date_not_future",
               f"first_authority_date={f.first_authority_date}")


def rule_13_accident_date_is_valid_past_date(f) -> RuleResult:
    """
    If accident_date is provided, it must be parseable and must be in the past.
    Date-filtered conclusions are meaningless for future dates.
    """
    if f.accident_date is None:
        return _na(13, "accident_date_is_valid_past_date", "no accident_date filter")
    d = _parse_date(f.accident_date)
    if d is None:
        return _fail(13, "accident_date_is_valid_past_date",
                     f"accident_date={f.accident_date!r} is not a parseable date")
    if d > date.today():
        return _fail(13, "accident_date_is_valid_past_date",
                     f"accident_date={f.accident_date} is in the future")
    return _ok(13, "accident_date_is_valid_past_date",
               f"accident_date={f.accident_date}")


def rule_14_no_prohibited_wording_in_status_fields(f) -> RuleResult:
    """
    Status fields must not contain prohibited zero-finding language
    ('no issues found', 'no authority issues found', etc.).
    These phrases belong nowhere in CARRIER_FACTS — they are narrative conclusions.
    """
    prohibited = [
        "no issues found",
        "no authority issues found",
        "no issue",
        "no problems found",
    ]
    check_fields = {
        "authority_status":  f.authority_status,
        "insurance_status":  f.insurance_status,
        "usdot_status":      f.usdot_status,
    }
    for fname, val in check_fields.items():
        v = (val or "").lower()
        for phrase in prohibited:
            if phrase in v:
                return _fail(14, "no_prohibited_wording_in_status_fields",
                             f"Prohibited phrase '{phrase}' found in {fname}={val!r}")
    return _ok(14, "no_prohibited_wording_in_status_fields")


def rule_15_no_overconfident_negative_from_empty_records(f) -> RuleResult:
    """
    CONFIRMED_LAPSED or CONFIRMED_REVOKED must never come from empty record sets.
    Empty records → NOT_FOUND, not a confirmed negative conclusion.
    """
    cf = _cf()
    if f.insurance_status == cf.CONFIRMED_LAPSED and not f._insurance_records:
        return _fail(15, "no_overconfident_negative_from_empty_records",
                     "insurance_status=CONFIRMED_LAPSED but _insurance_records is empty — "
                     "cannot confirm lapsed from zero records")
    if f.authority_status == cf.CONFIRMED_REVOKED and \
       not f._authority_records and not f._alerts:
        return _fail(15, "no_overconfident_negative_from_empty_records",
                     "authority_status=CONFIRMED_REVOKED but both _authority_records "
                     "and _alerts are empty — cannot confirm revoked from zero records")
    return _ok(15, "no_overconfident_negative_from_empty_records")


def rule_16_fleet_data_not_asymmetric(f) -> RuleResult:
    """
    If one of fleet_power_units / fleet_drivers is non-zero and the other is zero,
    flag it. Both non-zero is normal. Both zero is normal (inactive/new carrier).
    One non-zero + one zero suggests an import gap.
    This rule produces a warning (passed=False) but is not a blocking error.
    """
    pu = f.fleet_power_units
    dr = f.fleet_drivers
    if (pu > 0 and dr == 0) or (dr > 0 and pu == 0):
        return _fail(16, "fleet_data_not_asymmetric",
                     f"fleet asymmetry: power_units={pu}, drivers={dr} — "
                     f"one is 0 while the other is not (possible import gap)")
    return _ok(16, "fleet_data_not_asymmetric",
               f"power_units={pu}, drivers={dr}")


def rule_17_inspection_count_non_negative_int(f) -> RuleResult:
    """
    inspection_count must be a non-negative integer.
    The builder uses DISTINCT ON dedup, so the value is already deduplicated.
    """
    if not isinstance(f.inspection_count, int) or f.inspection_count < 0:
        return _fail(17, "inspection_count_non_negative_int",
                     f"inspection_count={f.inspection_count!r} is not a valid "
                     f"non-negative integer")
    return _ok(17, "inspection_count_non_negative_int",
               f"inspection_count={f.inspection_count}")


def rule_18_data_confidence_keys_present(f) -> RuleResult:
    """
    data_confidence dict must contain entries for all four required conclusions:
    authority, insurance, fleet, identity.
    Missing keys mean the confidence reporting is incomplete.
    """
    required = {"authority", "insurance", "fleet", "identity"}
    missing  = required - set(f.data_confidence.keys())
    if missing:
        return _fail(18, "data_confidence_keys_present",
                     f"data_confidence missing required keys: {sorted(missing)}")
    valid_values = {"HIGH", "MEDIUM", "LOW"}
    bad = {k: v for k, v in f.data_confidence.items()
           if k in required and v not in valid_values}
    if bad:
        return _fail(18, "data_confidence_keys_present",
                     f"data_confidence has invalid values: {bad}")
    return _ok(18, "data_confidence_keys_present",
               str({k: f.data_confidence[k] for k in required}))


def rule_19_for_hire_has_mc_or_auth_evidence(f) -> RuleResult:
    """
    FOR_HIRE_INTERSTATE classification requires at least one supporting signal:
    an MC number OR at least one authority history record.
    Without either, the classification is speculative and should be UNKNOWN.
    """
    cf = _cf()
    if f.carrier_type != cf.FOR_HIRE_INTERSTATE:
        return _na(19, "for_hire_has_mc_or_auth_evidence",
                   f"carrier_type={f.carrier_type}")
    has_mc   = bool(f.mc_number and f.mc_number != "MC")
    has_auth = len(f._authority_records) > 0
    if not has_mc and not has_auth:
        return _fail(19, "for_hire_has_mc_or_auth_evidence",
                     "carrier_type=FOR_HIRE_INTERSTATE but no MC number and no "
                     "authority_history records — classification needs evidence")
    return _ok(19, "for_hire_has_mc_or_auth_evidence",
               f"mc={f.mc_number or '—'}, auth_records={len(f._authority_records)}")


def rule_20_accident_date_parseable_if_set(f) -> RuleResult:
    """
    If accident_date is provided, it must be parseable as an ISO date.
    An unparseable date means date-filtered conclusions were computed against
    an invalid anchor — the report must not be generated.
    """
    if f.accident_date is None:
        return _na(20, "accident_date_parseable_if_set", "no accident_date set")
    d = _parse_date(f.accident_date)
    if d is None:
        return _fail(20, "accident_date_parseable_if_set",
                     f"accident_date={f.accident_date!r} could not be parsed as a date")
    return _ok(20, "accident_date_parseable_if_set",
               f"parsed={d.isoformat()}")


# ── Rule registry ─────────────────────────────────────────────────────────────

ALL_RULES: list[Callable] = [
    rule_01_authority_not_both_active_and_revoked,
    rule_02_private_no_authority_required,
    rule_03_private_no_insurance_required,
    rule_04_intrastate_not_rendered_as_revoked,
    rule_05_no_sms_without_inspections,
    rule_06_lapsed_requires_explicit_cancellation,
    rule_07_revoked_requires_revocation_event,
    rule_08_authority_status_is_valid_sentinel,
    rule_09_usdot_status_is_valid,
    rule_10_no_mc_placeholder,
    rule_11_insurance_dates_internally_consistent,
    rule_12_first_authority_date_not_future,
    rule_13_accident_date_is_valid_past_date,
    rule_14_no_prohibited_wording_in_status_fields,
    rule_15_no_overconfident_negative_from_empty_records,
    rule_16_fleet_data_not_asymmetric,
    rule_17_inspection_count_non_negative_int,
    rule_18_data_confidence_keys_present,
    rule_19_for_hire_has_mc_or_auth_evidence,
    rule_20_accident_date_parseable_if_set,
]


def run_validation(facts) -> list[RuleResult]:
    """
    Run all 20 rules against a CarrierFacts object.
    All rules run regardless of earlier failures — returns full picture.
    """
    results = []
    for rule_fn in ALL_RULES:
        try:
            results.append(rule_fn(facts))
        except Exception as exc:
            results.append(RuleResult(
                rule_id=0,
                rule_name=rule_fn.__name__,
                passed=False,
                message=f"Rule raised unhandled exception: {exc}",
            ))
    return results


# ── Conflict report helpers ───────────────────────────────────────────────────

# Maps each rule_name to the CarrierFacts fields it inspects.
_RULE_CONFLICT_FIELDS: dict = {
    "authority_not_both_active_and_revoked":        ["authority_status", "authority_revocation_date"],
    "private_no_authority_required":                ["carrier_type", "authority_required"],
    "private_no_insurance_required":                ["carrier_type", "insurance_required"],
    "intrastate_not_rendered_as_revoked":           ["carrier_type", "authority_status"],
    "no_sms_without_inspections":                   ["sms_percentile_present", "inspection_count"],
    "lapsed_requires_explicit_cancellation":        ["insurance_status", "insurance_cancellation"],
    "revoked_requires_revocation_event":            ["authority_status", "authority_revocation_date"],
    "authority_status_is_valid_sentinel":           ["authority_status"],
    "usdot_status_is_valid":                        ["usdot_status"],
    "no_mc_placeholder":                            ["mc_number"],
    "insurance_dates_internally_consistent":        ["insurance_status", "_insurance_records"],
    "first_authority_date_not_future":              ["first_authority_date"],
    "accident_date_is_valid_past_date":             ["accident_date"],
    "no_prohibited_wording_in_status_fields":       ["authority_status", "insurance_status", "usdot_status"],
    "no_overconfident_negative_from_empty_records": ["insurance_status", "authority_status"],
    "fleet_data_not_asymmetric":                    ["fleet_power_units", "fleet_drivers"],
    "inspection_count_non_negative_int":            ["inspection_count"],
    "data_confidence_keys_present":                 ["data_confidence"],
    "for_hire_has_mc_or_auth_evidence":             ["carrier_type", "mc_number"],
    "accident_date_parseable_if_set":               ["accident_date"],
}


def build_conflict_report(results: list) -> list:
    """
    Convert failed RuleResults into structured conflict dicts.
    Passing and N/A results are excluded.

    Returns list of dicts: [{"rule": ..., "fields": [...], "detail": ...}]
    """
    return [
        {
            "rule":   r.rule_name,
            "fields": _RULE_CONFLICT_FIELDS.get(r.rule_name, []),
            "detail": r.message,
        }
        for r in results
        if not r.passed
    ]


def run_validation_with_conflicts(facts) -> tuple:
    """
    Run all 20 rules and populate facts.validation_conflicts in-place.
    Returns (results, conflict_report).
    """
    results   = run_validation(facts)
    conflicts = build_conflict_report(results)
    if hasattr(facts, "validation_conflicts"):
        facts.validation_conflicts = conflicts
    return results, conflicts
