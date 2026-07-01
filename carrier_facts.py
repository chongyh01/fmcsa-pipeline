"""
carrier_facts.py
================
Layer 1 of the report pipeline: builds a CARRIER_FACTS object per DOT number.

Rules:
- Returns structured facts ONLY. No narrative, no legal conclusions, no prose.
- Every field is a known value OR an explicit sentinel (NOT_FOUND, REQUIRES_VERIFICATION).
- NEVER converts "no record found" into "no insurance / no authority / no issue".
- All DB queries live here. Validators and narrative consume the object only.

Usage:
    from carrier_facts import build_carrier_facts
    conn = psycopg2.connect(DB_URL)
    facts = build_carrier_facts(conn, "204814", accident_date="2026-01-15")
"""

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# ── Sentinels ─────────────────────────────────────────────────────────────────
NOT_FOUND             = "NOT_FOUND"
REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"

# carrier_type
FOR_HIRE_INTERSTATE  = "FOR_HIRE_INTERSTATE"
MIXED_OPERATION      = "MIXED_OPERATION"      # for-hire + private both present
PRIVATE              = "PRIVATE"
INTRASTATE           = "INTRASTATE"           # legacy; prefer INTRASTATE_ONLY for new carriers
INTRASTATE_ONLY      = "INTRASTATE_ONLY"      # authorised for hire at state level, no federal MC
PASSENGER            = "PASSENGER"
UNKNOWN              = "UNKNOWN"

# authority_status / insurance_status
CONFIRMED_ACTIVE  = "CONFIRMED_ACTIVE"
CONFIRMED_REVOKED = "CONFIRMED_REVOKED"
CONFIRMED_LAPSED  = "CONFIRMED_LAPSED"
NOT_REQUIRED      = "NOT_REQUIRED"

# data_confidence
HIGH   = "HIGH"
MEDIUM = "MEDIUM"
LOW    = "LOW"


# ── Data object ───────────────────────────────────────────────────────────────

@dataclass
class CarrierFacts:
    dot_number:               str
    mc_number:                Optional[str]   # None if no MC — never "MC" placeholder
    legal_name:               str
    carrier_type:             str             # FOR_HIRE_INTERSTATE | MIXED_OPERATION | PRIVATE | INTRASTATE_ONLY | INTRASTATE | PASSENGER | UNKNOWN | REQUIRES_VERIFICATION
    usdot_status:             str             # ACTIVE | INACTIVE | NOT AUTHORIZED | OUT-OF-SERVICE | NOT_FOUND
    authority_required:       str             # YES | NO | REQUIRES_VERIFICATION
    authority_status:         str             # CONFIRMED_ACTIVE | CONFIRMED_REVOKED | NOT_REQUIRED | NOT_FOUND | REQUIRES_VERIFICATION
    authority_revocation_date: Optional[str]  # ISO date or None; set ONLY for CONFIRMED_REVOKED (never for CONFIRMED_ACTIVE)
    first_authority_date:     Optional[str]   # ISO date or None
    active_authority_period:  Optional[str]   # human-readable span or None
    insurance_required:       str             # YES | NO | REQUIRES_VERIFICATION
    insurance_status:         str             # CONFIRMED_ACTIVE | CONFIRMED_LAPSED | NOT_REQUIRED | NOT_FOUND | REQUIRES_VERIFICATION
    insurance_cancellation:   str             # YES | NO | NOT_FOUND | NOT_REQUIRED
    insurance_replacement:    str             # YES | NO | NOT_FOUND | NOT_REQUIRED
    fleet_power_units:        int
    fleet_drivers:            int
    inspection_count:         int             # deduped via DISTINCT ON (inspection_date, state, level)
    crash_count:              int
    violation_count:          int
    boc3_on_file:             str             # YES | NO | NOT_FOUND
    sms_percentile_present:   bool
    accident_date:            Optional[str]   # ISO date, the filter this report is built around
    data_confidence:          dict            # keys: authority, insurance, fleet, identity → HIGH|MEDIUM|LOW
    # Raw records for validators — not for narrative
    _authority_records:   list = field(default_factory=list, repr=False)
    _insurance_records:   list = field(default_factory=list, repr=False)
    _alerts:              list = field(default_factory=list, repr=False)
    validation_conflicts: list = field(default_factory=list, repr=False)


# ── Date helpers ──────────────────────────────────────────────────────────────

def _parse_date(val) -> Optional[date]:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(val)[:10], fmt).date()
        except ValueError:
            continue
    return None


def _fmt(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


# ── Carrier-type inference ────────────────────────────────────────────────────

def _infer_carrier_type(carrier: dict, authority_records: list) -> str:
    """
    Infer carrier_type from cargo_type (multi-value), MC number, and authority records.

    cargo_type is a semicolon-delimited field from the FMCSA carrier census — a carrier
    may have multiple operation classifications simultaneously (e.g. both
    "PRIVATE PROPERTY" and "AUTHORIZED FOR HIRE").  The old substring match on the
    joined string incorrectly treated "PRIVATE PROPERTY;AUTHORIZED FOR HIRE" as PRIVATE.

    Priority order:
    1. PASSENGER signal in any cargo_type part → PASSENGER
    2. PRIVATE + interstate for-hire (has_mc OR federal auth records) → MIXED_OPERATION
    3. Interstate for-hire (no private) → FOR_HIRE_INTERSTATE
    4. PRIVATE only (no for-hire flag, or for-hire flag but no MC/federal auth) → PRIVATE
    5. FOR-HIRE flag present but NO MC and NO federal auth records → INTRASTATE_ONLY
       (state-authorised carrier; no federal FMCSA docket number)
    6. INTRASTATE flag in cargo → INTRASTATE_ONLY
    7. MC number present with no cargo flag (data quality gap) → FOR_HIRE_INTERSTATE
    8. Default → REQUIRES_VERIFICATION (genuinely unknown; never guess)
    """
    mc        = (carrier.get("mc_number") or "").strip()
    cargo_raw = (carrier.get("cargo_type") or "")
    parts     = {p.strip().upper() for p in cargo_raw.split(";") if p.strip()}

    has_passenger  = any("PASSENGER" in p for p in parts)
    has_for_hire   = any(
        "AUTHORIZED FOR HIRE" in p or "AUTH. FOR HIRE" in p or "AUTH FOR HIRE" in p
        for p in parts
    )
    has_private    = any("PRIVATE" in p for p in parts)
    has_intrastate = any("INTRASTATE" in p for p in parts)
    has_mc         = bool(mc and mc != "MC" and len(mc) > 2)

    # Federal authority evidence: any GRANTED or REINSTATED record (not DISCONTINUED).
    # authority_type is NULL in our import — use status field instead.
    has_federal_auth = any(
        "GRANT" in (rec.get("status") or "").upper() or
        "REINSTAT" in (rec.get("status") or "").upper()
        for rec in authority_records
        if "DISCONTIN" not in (rec.get("reason") or "").upper()
    )

    # Interstate for-hire = cargo says for-hire AND carrier has federal presence
    # (MC docket number or FMCSA authority records).
    is_interstate_fh = has_for_hire and (has_mc or has_federal_auth)

    if has_passenger:
        return PASSENGER

    if has_private and is_interstate_fh:
        # Both operation types present — carrier operates in both modes.
        return MIXED_OPERATION

    if is_interstate_fh:
        return FOR_HIRE_INTERSTATE

    if has_private:
        # Pure private: private cargo classification, no interstate for-hire evidence.
        # An MC number in the DB without a for-hire cargo flag is treated as an import
        # artifact (TREDZ CENTRAL / PUBLIC SERVICE NC pattern).
        return PRIVATE

    # For-hire flag present but no MC and no federal authority records
    # → operating under state authority only, not FMCSA interstate authority.
    if has_for_hire:
        return INTRASTATE_ONLY

    if has_intrastate:
        return INTRASTATE_ONLY

    # No recognised cargo flags — fall back to MC number as last resort.
    if has_mc:
        return FOR_HIRE_INTERSTATE

    return REQUIRES_VERIFICATION


# ── Authority status inference ────────────────────────────────────────────────

def _infer_authority_status(authority_records: list, alerts: list) -> tuple:
    """
    Returns (status, revocation_date_str, first_authority_date_str, active_period_str).

    CONFIRMED_ACTIVE:  has GRANTED authority with no un-reversed INVOLUNTARY REVOCATION,
                       OR has a REINSTATEMENT / new GRANT after the most recent revocation.
    CONFIRMED_REVOKED: has INVOLUNTARY REVOCATION (not discontinued) with no subsequent
                       REINSTATEMENT or new GRANT.
    NOT_FOUND:         no authority records at all.
    REQUIRES_VERIFICATION: conflicting signals.

    Critical: CONFIRMED_REVOKED requires a real revocation event — not absence of records.
    """
    if not authority_records and not alerts:
        return NOT_FOUND, None, None, None

    grants         = []
    revocations    = []
    reinstatements = []

    for rec in authority_records:
        status = (rec.get("status") or "").upper()
        reason = (rec.get("reason") or "").upper()
        eff    = _parse_date(rec.get("effective_date"))

        if "GRANT" in status:
            grants.append(eff)
        elif "REVOC" in status or "INVOLUNTARY" in status:
            # Skip DISCONTINUED (reversed/withdrawn) revocations
            if "DISCONTIN" not in status and "DISCONTIN" not in reason:
                revocations.append(eff)
        elif "REINSTAT" in status:
            reinstatements.append(eff)

    for alert in alerts:
        etype = (alert.get("event_type") or "").upper()
        desc  = (alert.get("description") or "").upper()
        edate = _parse_date(alert.get("event_date"))
        if "INVOLUNTARY_REVOCATION" in etype and "DISCONTIN" not in desc:
            revocations.append(edate)

    grants         = sorted([d for d in grants         if d])
    revocations    = sorted([d for d in revocations    if d])
    reinstatements = sorted([d for d in reinstatements if d])

    first_grant         = grants[0]         if grants         else None
    latest_revocation   = revocations[-1]   if revocations    else None
    latest_reinstatement = reinstatements[-1] if reinstatements else None
    latest_grant        = grants[-1]        if grants         else None

    # Determine status
    if revocations:
        # Reversed by subsequent reinstatement or new grant?
        post_revoc_event = (
            (latest_reinstatement and latest_reinstatement > latest_revocation) or
            (latest_grant         and latest_grant         > latest_revocation)
        )
        status_out = CONFIRMED_ACTIVE if post_revoc_event else CONFIRMED_REVOKED
    elif grants or reinstatements:
        status_out = CONFIRMED_ACTIVE
    else:
        # authority_records exist but none have classifiable statuses
        status_out = REQUIRES_VERIFICATION

    # Compute active_authority_period
    active_period = None
    if first_grant:
        if status_out == CONFIRMED_REVOKED and latest_revocation:
            days  = (latest_revocation - first_grant).days
            years = days / 365.25
            active_period = (f"{_fmt(first_grant)} to {_fmt(latest_revocation)} "
                             f"({years:.0f} yr)")
        elif status_out == CONFIRMED_ACTIVE:
            today = date.today()
            days  = (today - first_grant).days
            years = days / 365.25
            active_period = f"{_fmt(first_grant)} to present ({years:.0f} yr)"

    # authority_revocation_date is set ONLY for CONFIRMED_REVOKED.
    # For CONFIRMED_ACTIVE carriers with a historical revocation that was later
    # reversed by a reinstatement or new grant, the past revocation_date is not
    # the current authority state.  Historical events remain in _authority_records.
    revoc_date_out = _fmt(latest_revocation) if status_out == CONFIRMED_REVOKED else None
    return (
        status_out,
        revoc_date_out,
        _fmt(first_grant),
        active_period,
    )


# ── Insurance status inference ────────────────────────────────────────────────

def _infer_insurance_status(insurance_records: list) -> tuple:
    """
    Returns (insurance_status, insurance_cancellation, insurance_replacement).

    CONFIRMED_ACTIVE:  has at least one policy with status ACTIVE (or no cancellation_date
                       and not cancelled/replaced).
    CONFIRMED_LAPSED:  all policies are cancelled, none replaced — EXPLICIT cancellation proof.
    REQUIRES_VERIFICATION: has cancelled + replaced mix (replacement may be active elsewhere)
                           or other ambiguous signals.
    NOT_FOUND:         no records at all.

    CRITICAL: CONFIRMED_LAPSED requires explicit cancellation evidence.
    "No records" → NOT_FOUND, not CONFIRMED_LAPSED.
    """
    if not insurance_records:
        return NOT_FOUND, NOT_FOUND, NOT_FOUND

    has_active    = False
    has_cancelled = False
    has_replaced  = False

    for rec in insurance_records:
        s = (rec.get("status") or "").upper()
        cancel_date = _parse_date(rec.get("cancellation_date"))

        if "ACTIVE" in s:
            has_active = True
        elif "CANCEL" in s:
            has_cancelled = True
        elif "REPLAC" in s:
            has_replaced = True
        elif not cancel_date:
            # No cancellation date and no explicit status = treat as active
            has_active = True

    if has_active:
        ins_status = CONFIRMED_ACTIVE
    elif has_cancelled and has_replaced:
        # Replaced policies indicate a successor policy — ambiguous without it in our DB
        ins_status = REQUIRES_VERIFICATION
    elif has_cancelled:
        ins_status = CONFIRMED_LAPSED
    elif has_replaced:
        # All replaced — successor should be somewhere; can't confirm lapsed
        ins_status = REQUIRES_VERIFICATION
    else:
        ins_status = REQUIRES_VERIFICATION

    return (
        ins_status,
        "YES" if has_cancelled else ("NO" if insurance_records else NOT_FOUND),
        "YES" if has_replaced  else ("NO" if insurance_records else NOT_FOUND),
    )


# ── Main builder ──────────────────────────────────────────────────────────────

def build_carrier_facts(conn, dot_number: str, accident_date: Optional[str] = None) -> CarrierFacts:
    """
    Build and return a CarrierFacts object for the given DOT number.

    Args:
        conn:          live psycopg2 connection (caller owns lifecycle)
        dot_number:    USDOT number as string
        accident_date: optional ISO date string "YYYY-MM-DD" for date filtering

    Returns CarrierFacts with all fields populated (never raises on missing data).
    """
    acc_date = _parse_date(accident_date)

    cur = conn.cursor()
    cur.execute("SET statement_timeout = '30s'")

    # ── 1. Carrier base row ───────────────────────────────────────────────────
    cur.execute("""
        SELECT dot_number, mc_number, legal_name, status,
               total_drivers, total_trucks, cargo_type,
               safety_rating, safety_rating_date
        FROM carriers WHERE dot_number = %s
    """, (dot_number,))
    row = cur.fetchone()

    if not row:
        cur.close()
        return CarrierFacts(
            dot_number=dot_number, mc_number=None, legal_name=NOT_FOUND,
            carrier_type=UNKNOWN, usdot_status=NOT_FOUND,
            authority_required=NOT_FOUND, authority_status=NOT_FOUND,
            authority_revocation_date=None, first_authority_date=None,
            active_authority_period=None, insurance_required=NOT_FOUND,
            insurance_status=NOT_FOUND, insurance_cancellation=NOT_FOUND,
            insurance_replacement=NOT_FOUND, fleet_power_units=0, fleet_drivers=0,
            inspection_count=0, crash_count=0, violation_count=0,
            boc3_on_file=NOT_FOUND, sms_percentile_present=False,
            accident_date=accident_date,
            data_confidence={"authority": LOW, "insurance": LOW, "fleet": LOW, "identity": LOW},
        )

    cols    = ["dot_number", "mc_number", "legal_name", "status",
               "total_drivers", "total_trucks", "cargo_type",
               "safety_rating", "safety_rating_date"]
    carrier = dict(zip(cols, row))

    # Sanitize MC number — "MC" placeholder (no numeric suffix) → None
    mc = (carrier.get("mc_number") or "").strip()
    if not mc or mc == "MC":
        mc = None
    carrier["mc_number"] = mc

    # ── 2. Authority history ──────────────────────────────────────────────────
    cur.execute("""
        SELECT authority_type, status, effective_date, revocation_date, reason
        FROM authority_history WHERE dot_number = %s
        ORDER BY effective_date DESC NULLS LAST
    """, (dot_number,))
    auth_cols       = ["authority_type", "status", "effective_date", "revocation_date", "reason"]
    authority_records = [dict(zip(auth_cols, r)) for r in cur.fetchall()]

    # ── 3. Carrier alerts (revocations etc.) ─────────────────────────────────
    cur.execute("""
        SELECT event_type, event_date, description
        FROM carrier_alerts WHERE dot_number = %s
        ORDER BY event_date DESC NULLS LAST
    """, (dot_number,))
    alert_cols = ["event_type", "event_date", "description"]
    alerts     = [dict(zip(alert_cols, r)) for r in cur.fetchall()]

    # ── 4. Insurance records ──────────────────────────────────────────────────
    cur.execute("""
        SELECT policy_type, insurer_name, policy_number,
               effective_date, cancellation_date, status
        FROM insurance WHERE dot_number = %s
        ORDER BY effective_date DESC NULLS LAST
    """, (dot_number,))
    ins_cols         = ["policy_type", "insurer_name", "policy_number",
                        "effective_date", "cancellation_date", "status"]
    insurance_records = [dict(zip(ins_cols, r)) for r in cur.fetchall()]

    # ── 5. Counts ─────────────────────────────────────────────────────────────
    # Deduped inspection count — distinct on (inspection_date, state, level)
    # to avoid inflated OOS rates from duplicate rows in the source data
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT inspection_date, state, level
            FROM inspections WHERE dot_number = %s
        ) AS deduped
    """, (dot_number,))
    inspection_count = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM crashes    WHERE dot_number = %s", (dot_number,))
    crash_count = cur.fetchone()[0] or 0

    cur.execute("SELECT COUNT(*) FROM violations WHERE dot_number = %s", (dot_number,))
    violation_count = cur.fetchone()[0] or 0

    # ── 6. BOC-3 ─────────────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM boc3 WHERE dot_number = %s", (dot_number,))
    boc3_on_file = "YES" if (cur.fetchone()[0] or 0) > 0 else "NO"

    # ── 7. SMS scores ─────────────────────────────────────────────────────────
    cur.execute("""
        SELECT unsafe_driving, hours_of_service_compliance, driver_fitness,
               controlled_substances_alcohol, vehicle_maintenance,
               hazardous_materials, crash_indicator
        FROM sms_scores WHERE dot_number = %s
        ORDER BY score_date DESC NULLS LAST LIMIT 1
    """, (dot_number,))
    sms_row = cur.fetchone()
    sms_percentile_present = sms_row is not None and any(v is not None for v in sms_row)

    cur.close()

    # ── Derive structured facts ───────────────────────────────────────────────
    carrier_type = _infer_carrier_type(carrier, authority_records)

    raw_status   = (carrier.get("status") or "").upper().strip()
    valid_statuses = {"ACTIVE", "INACTIVE", "NOT AUTHORIZED", "OUT-OF-SERVICE"}
    usdot_status = raw_status if raw_status in valid_statuses else NOT_FOUND

    # Authority and insurance requirements
    if carrier_type in (PRIVATE, INTRASTATE, INTRASTATE_ONLY):
        # No federal FMCSA authority or insurance filing required.
        # INTRASTATE_ONLY carriers operate under state authority — display the
        # carrier type explicitly rather than implying no regulation applies.
        authority_required = "NO"
        insurance_required = "NO"
    elif carrier_type in (UNKNOWN, REQUIRES_VERIFICATION):
        authority_required = REQUIRES_VERIFICATION
        insurance_required = REQUIRES_VERIFICATION
    else:
        # FOR_HIRE_INTERSTATE, MIXED_OPERATION, PASSENGER — all have for-hire exposure.
        # MIXED_OPERATION must show authority/insurance status even though it also does
        # private hauling; the for-hire portion triggers federal filing requirements.
        authority_required = "YES"
        insurance_required = "YES"

    auth_status, auth_rev_date, first_auth_date, active_auth_period = \
        _infer_authority_status(authority_records, alerts)

    if authority_required == "NO":
        auth_status       = NOT_REQUIRED
        auth_rev_date     = None
        first_auth_date   = None
        active_auth_period = None

    ins_status, ins_cancel, ins_replace = _infer_insurance_status(insurance_records)
    if insurance_required == "NO":
        ins_status  = NOT_REQUIRED
        ins_cancel  = NOT_REQUIRED
        ins_replace = NOT_REQUIRED

    # ── Data confidence ───────────────────────────────────────────────────────
    def _auth_confidence():
        if authority_required == "NO":
            return HIGH
        if auth_status in (CONFIRMED_ACTIVE, CONFIRMED_REVOKED) and authority_records:
            return HIGH
        if auth_status == NOT_FOUND:
            return LOW
        return MEDIUM

    def _ins_confidence():
        if insurance_required == "NO":
            return HIGH
        if ins_status in (CONFIRMED_ACTIVE, CONFIRMED_LAPSED) and insurance_records:
            return HIGH
        if ins_status == NOT_FOUND:
            return LOW
        return MEDIUM

    confidence = {
        "authority": _auth_confidence(),
        "insurance": _ins_confidence(),
        "fleet":     HIGH if carrier.get("total_trucks") is not None else LOW,
        "identity":  HIGH if carrier.get("legal_name") else LOW,
    }

    return CarrierFacts(
        dot_number=dot_number,
        mc_number=mc or None,
        legal_name=carrier.get("legal_name") or NOT_FOUND,
        carrier_type=carrier_type,
        usdot_status=usdot_status,
        authority_required=authority_required,
        authority_status=auth_status,
        authority_revocation_date=auth_rev_date,
        first_authority_date=first_auth_date,
        active_authority_period=active_auth_period,
        insurance_required=insurance_required,
        insurance_status=ins_status,
        insurance_cancellation=ins_cancel,
        insurance_replacement=ins_replace,
        fleet_power_units=carrier.get("total_trucks") or 0,
        fleet_drivers=carrier.get("total_drivers") or 0,
        inspection_count=inspection_count,
        crash_count=crash_count,
        violation_count=violation_count,
        boc3_on_file=boc3_on_file,
        sms_percentile_present=sms_percentile_present,
        accident_date=accident_date,
        data_confidence=confidence,
        _authority_records=authority_records,
        _insurance_records=insurance_records,
        _alerts=alerts,
    )
