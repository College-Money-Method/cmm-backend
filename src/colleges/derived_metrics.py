"""Pure derived metrics and the public item shape for the College Aid Explorer.

Only source facts are stored; these are computed at read time with Decimal
(4 dp). Every metric is None when an input is None or the divisor is 0.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from src.colleges.models import College, CollegeAidRecord
from src.colleges.public_schemas import CollegeAidItem

_FOUR_DP = Decimal("0.0001")
_ONE = Decimal(1)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_FOUR_DP, rounding=ROUND_HALF_UP)


def pct_need_unmet(pct_need_met: Decimal | None) -> Decimal | None:
    """Share of need left unmet: 1 - pct_need_met."""
    return None if pct_need_met is None else _q(_ONE - pct_need_met)


def pct_non_need_paying_full_coa(pct_receiving_merit: Decimal | None) -> Decimal | None:
    """Workbook label kept: students getting no merit aid, 1 - pct receiving merit."""
    return None if pct_receiving_merit is None else _q(_ONE - pct_receiving_merit)


def merit_pct_of_coa(avg_merit_award: Decimal | None, coa: Decimal | None) -> Decimal | None:
    """Average merit award as a fraction of cost of attendance."""
    if avg_merit_award is None or coa is None or coa == 0:
        return None
    return _q(avg_merit_award / coa)


def normalize_aid_form(raw: str | None) -> str | None:
    """Map the raw 'Application for Institutional Aid' text to an enum.

    CSS wins when both forms are named (the stricter requirement); anything
    present but unrecognized is "other"; blank is None.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if "css" in text:
        return "css"
    if "fafsa" in text:
        return "fafsa"
    if "institution" in text or "form" in text:
        return "institutional"
    return "other"


def to_public_item(college: College, record: CollegeAidRecord) -> CollegeAidItem:
    """Join a college with its chosen record and attach the derived metrics."""
    return CollegeAidItem(
        college_id=college.id,
        name=college.name,
        state=college.state,
        region=college.region,
        control=college.control,
        cds_year=record.cds_year,
        release_label=record.release_label,
        source=record.source,
        coa_in_state=record.coa_in_state,
        coa_out_of_state=record.coa_out_of_state,
        coa_year=record.coa_year,
        coa_estimated=record.coa_estimated,
        full_time_undergrads=record.full_time_undergrads,
        aid_application=record.aid_application,
        aid_form=normalize_aid_form(record.aid_application),
        pct_need_met=record.pct_need_met,
        pct_non_need_receiving_merit=record.pct_non_need_receiving_merit,
        avg_merit_award=record.avg_merit_award,
        pct_need_unmet=pct_need_unmet(record.pct_need_met),
        pct_non_need_paying_full_coa=pct_non_need_paying_full_coa(record.pct_non_need_receiving_merit),
        merit_pct_of_coa_out_of_state=merit_pct_of_coa(record.avg_merit_award, record.coa_out_of_state),
        merit_pct_of_coa_in_state=merit_pct_of_coa(record.avg_merit_award, record.coa_in_state),
    )
