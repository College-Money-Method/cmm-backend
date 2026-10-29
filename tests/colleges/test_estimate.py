"""Pure estimate: published-package fixtures plus the formula's edge branches."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.colleges.estimate import (
    GIFT_SHARE_BY_CONTROL,
    CollegeAidFacts,
    EstimateInputs,
    estimate_for_college,
)
from tests.colleges.fixtures.net_price_real_cases import (
    NET_PRICE_REAL_CASES,
    NO_NEED_REAL_CASE,
)


def facts(control="private", state="NY", coa_in=None, coa_out=Decimal(80000), pct="0.8", merit=Decimal(10000)):
    return CollegeAidFacts(
        control=control, state=state, coa_in_state=coa_in, coa_out_of_state=coa_out, coa_year="2025-26",
        pct_need_met=None if pct is None else Decimal(pct), avg_merit_award=merit,
    )


def inputs(sai=20000, home=None, mode="none", amount=None):
    return EstimateInputs(sai=sai, home_state=home, merit_mode=mode, merit_amount=amount)


@pytest.mark.parametrize("case", NET_PRICE_REAL_CASES, ids=lambda c: f"{c[0]}-{c[3]}-{c[5]}")
def test_real_cases_fall_inside_range(case):
    _, control, coa, sai, pct, merit, actual, _ = case
    est = estimate_for_college(
        facts(control=control, coa_out=Decimal(coa), pct=str(pct), merit=None),
        inputs(sai=sai, mode="amount", amount=Decimal(merit)),
    )
    assert est.net_low <= actual <= est.net_high


def test_no_need_real_case_collapses_to_coa_minus_merit():
    _, control, coa, _, _, merit, actual, _ = NO_NEED_REAL_CASE
    est = estimate_for_college(
        facts(control=control, coa_out=Decimal(coa), pct=None, merit=None),
        inputs(sai=coa + 5000, mode="amount", amount=Decimal(merit)),
    )
    assert est.need == 0
    assert (est.net_low, est.net_high) == (actual, actual)
    assert est.missing == []


def test_negative_sai_floored_to_zero():
    neg = estimate_for_college(facts(), inputs(sai=-1500))
    zero = estimate_for_college(facts(), inputs(sai=0))
    assert neg == zero
    assert neg.need == 80000


def test_sai_above_coa_means_no_need():
    est = estimate_for_college(facts(pct=None), inputs(sai=90000, mode="avg"))
    assert est.need == 0 and est.need_package == 0
    assert est.net_low == est.net_high == 70000  # COA - avg merit
    assert est.family_low == est.family_high == 70000


def test_public_in_state_out_of_state_and_no_home():
    f = facts(control="public", state="FL", coa_in=Decimal(20000), coa_out=Decimal(40000))
    assert estimate_for_college(f, inputs(home="FL")).coa_basis == "in_state"
    assert estimate_for_college(f, inputs(home="FL")).coa_used == 20000
    assert estimate_for_college(f, inputs(home="fl")).coa_basis == "in_state"
    out = estimate_for_college(f, inputs(home="GA"))
    assert (out.coa_basis, out.coa_used) == ("out_of_state", 40000)
    none = estimate_for_college(f, inputs(home=None))
    assert (none.coa_basis, none.coa_used) == ("out_of_state", 40000)


def test_private_ignores_home_state():
    f = facts(control="private", state="NY", coa_in=Decimal(1000))
    assert estimate_for_college(f, inputs(home="NY")).coa_basis == "out_of_state"


def test_missing_coa_nulls_everything():
    est = estimate_for_college(facts(coa_out=None), inputs())
    assert est.coa_used is None and est.need is None and est.net_low is None and est.family_high is None
    assert est.missing == ["coa"]


def test_public_in_state_coa_missing_does_not_fall_back():
    f = facts(control="public", state="FL", coa_in=None, coa_out=Decimal(40000))
    assert estimate_for_college(f, inputs(home="FL")).missing == ["coa"]


def test_missing_pct_with_need_nulls_package_but_keeps_coa_and_need():
    est = estimate_for_college(facts(pct=None), inputs(mode="avg"))
    assert est.missing == ["pct_need_met"]
    assert est.coa_used == 80000 and est.need == 60000 and est.merit_used == 10000
    assert est.need_package is None and est.net_low is None and est.self_help_low is None
    assert est.family_low is None


def test_missing_avg_merit_nulls_merit_dependent_outputs():
    est = estimate_for_college(facts(merit=None), inputs(mode="avg"))
    assert est.missing == ["avg_merit_award"]
    assert est.merit_used is None and est.net_low is None and est.family_low is None
    assert est.need_package == 48000  # not merit dependent


def test_merit_none_or_amount_ignores_missing_avg():
    assert estimate_for_college(facts(merit=None), inputs(mode="none")).missing == []
    assert estimate_for_college(facts(merit=None), inputs(mode="amount", amount=Decimal(5000))).merit_used == 5000


def test_merit_none_range_is_need_grant_spread():
    est = estimate_for_college(facts(), inputs(sai=20000))
    lo, hi = GIFT_SHARE_BY_CONTROL["private"]
    pkg = Decimal(48000)
    assert est.net_low == 80000 - hi * pkg  # 32960
    assert est.net_high == 80000 - lo * pkg  # 41120
    assert est.merit_used == 0


def test_family_never_exceeds_net_price():
    for mode, amount in (("none", None), ("avg", None), ("amount", Decimal(30000)), ("amount", Decimal(120000))):
        for sai in (-1500, 0, 15000, 60000, 200000):
            for control in ("private", "public"):
                est = estimate_for_college(facts(control=control), inputs(sai=sai, mode=mode, amount=amount))
                assert est.family_low <= est.net_low and est.family_high <= est.net_high
                assert est.net_low <= est.net_high and est.family_low <= est.family_high


def test_rounding_outward_for_ranges_and_half_up_for_singles():
    est = estimate_for_college(
        facts(coa_out=Decimal("80004.99"), pct="0.8333"), inputs(sai=1234, mode="amount", amount=Decimal(1005)),
    )
    for v in (est.net_low, est.net_high, est.family_low, est.family_high, est.self_help_low, est.self_help_high):
        assert v % 10 == 0
    assert est.coa_used == 80000  # half-up single
    assert est.merit_used == 1010  # 1005 -> half-up
    # Unrounded net_low = COA - hi*A - M is not a multiple of 10 here; outward means floor.
    need = Decimal("80004.99") - 1234
    pkg = Decimal("0.8333") * need
    raw_low = Decimal("80004.99") - Decimal("0.98") * pkg - 1005
    raw_high = Decimal("80004.99") - max(min(Decimal("0.81") * pkg + 1005, pkg), Decimal(1005))
    assert est.net_low <= raw_low < est.net_low + 10
    assert est.net_high - 10 < raw_high <= est.net_high


def test_zero_floor_on_large_merit():
    est = estimate_for_college(facts(), inputs(mode="amount", amount=Decimal(500000)))
    assert (est.net_low, est.net_high, est.family_low, est.family_high) == (0, 0, 0, 0)
