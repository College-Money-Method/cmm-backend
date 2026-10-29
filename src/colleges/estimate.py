"""Pure student aid estimate for one college (no DB, no HTTP; a chatbot tool can reuse it).

Decimal throughout. Outputs are rounded to $10: ranges outward (low floored, high
ceiled), single values half-up. Missing source data yields None, never 0, and the
item's ``missing`` list names what was absent so the UI can say "Not enough data".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Literal

# Share of the need-based package (grants + subsidized loans + work-study) that is
# grant money, per control. Fitted to published sample aid packages (see
# cmm-frontend plans/reports/researcher-261029-1404-net-price-evidence-widening.md,
# section 3, option 3): the union range covers both readings of Centre College's
# "% need met" (92% vs the school's 100% claim) and UF's Excellence Grant being
# classed as merit or need, so every fixture lands inside [net_low, net_high].
GIFT_SHARE_BY_CONTROL: dict[str, tuple[Decimal, Decimal]] = {
    "private": (Decimal("0.81"), Decimal("0.98")),
    "public": (Decimal("0.34"), Decimal("0.79")),
}

MeritMode = Literal["none", "avg", "amount"]

_ZERO = Decimal(0)
_TEN = Decimal(10)


@dataclass(frozen=True)
class CollegeAidFacts:
    control: Literal["public", "private"]
    state: str
    coa_in_state: Decimal | None
    coa_out_of_state: Decimal | None
    coa_year: str | None
    pct_need_met: Decimal | None
    avg_merit_award: Decimal | None


@dataclass(frozen=True)
class EstimateInputs:
    sai: int
    home_state: str | None = None
    merit_mode: MeritMode = "avg"
    merit_amount: Decimal | None = None  # used only when merit_mode == "amount"


@dataclass(frozen=True)
class EstimateResult:
    coa_used: Decimal | None = None
    coa_basis: Literal["in_state", "out_of_state"] | None = None
    coa_year: str | None = None
    need: Decimal | None = None
    need_package: Decimal | None = None
    merit_used: Decimal | None = None
    net_low: Decimal | None = None
    net_high: Decimal | None = None
    self_help_low: Decimal | None = None
    self_help_high: Decimal | None = None
    family_low: Decimal | None = None
    family_high: Decimal | None = None
    missing: list[str] = field(default_factory=list)


def _round(value: Decimal) -> Decimal:
    """Single values: nearest $10, half-up."""
    return (value / _TEN).to_integral_value(ROUND_HALF_UP) * _TEN


def _low(value: Decimal) -> Decimal:
    return (max(_ZERO, value) / _TEN).to_integral_value(ROUND_FLOOR) * _TEN


def _high(value: Decimal) -> Decimal:
    return (max(_ZERO, value) / _TEN).to_integral_value(ROUND_CEILING) * _TEN


def _pick_coa(facts: CollegeAidFacts, home_state: str | None):
    in_state = facts.control == "public" and home_state is not None and home_state == facts.state
    if in_state:
        return facts.coa_in_state, "in_state"
    return facts.coa_out_of_state, "out_of_state"


def estimate_for_college(facts: CollegeAidFacts, inputs: EstimateInputs) -> EstimateResult:
    missing: list[str] = []
    home = inputs.home_state.upper() if inputs.home_state else None
    coa, basis = _pick_coa(facts, home)

    merit: Decimal | None
    if inputs.merit_mode == "none":
        merit = _ZERO
    elif inputs.merit_mode == "amount":
        merit = max(_ZERO, inputs.merit_amount or _ZERO)
    else:
        merit = facts.avg_merit_award
        if merit is None:
            missing.append("avg_merit_award")

    if coa is None:
        return EstimateResult(missing=["coa", *missing])

    need = max(_ZERO, coa - max(_ZERO, Decimal(inputs.sai)))
    base = {"coa_used": _round(coa), "coa_basis": basis, "coa_year": facts.coa_year, "need": _round(need)}
    merit_used = None if merit is None else _round(merit)

    if need > 0 and facts.pct_need_met is None:
        return EstimateResult(**base, merit_used=merit_used, missing=["pct_need_met", *missing])

    pkg = min(need, (facts.pct_need_met or _ZERO) * need)  # no need -> no package, pct irrelevant
    share_lo, share_hi = GIFT_SHARE_BY_CONTROL[facts.control]
    g_lo, g_hi = share_lo * pkg, share_hi * pkg
    result = {**base, "need_package": _round(pkg), "merit_used": merit_used,
              "self_help_low": _low(pkg - g_hi), "self_help_high": _high(pkg - g_lo)}
    if merit is None:
        return EstimateResult(**result, missing=missing)
    return EstimateResult(
        **result,
        net_low=_low(coa - g_hi - merit),
        net_high=_high(coa - max(min(g_lo + merit, pkg), merit)),
        family_low=_low(coa - pkg - merit),
        family_high=_high(coa - max(pkg, merit)),
        missing=missing,
    )
