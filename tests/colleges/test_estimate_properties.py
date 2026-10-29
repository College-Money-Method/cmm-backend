"""Invariants of the estimate formula over the published fixtures and a SAI / merit grid."""

from __future__ import annotations

from decimal import Decimal

from src.colleges.estimate import (
    CollegeAidFacts,
    EstimateInputs,
    EstimateResult,
    estimate_for_college,
)
from tests.colleges.fixtures.net_price_real_cases import (
    NET_PRICE_REAL_CASES,
    NO_NEED_REAL_CASE,
)

RANGE_FIELDS = ("net_low", "net_high", "family_low", "family_high", "self_help_low", "self_help_high")
SAI_GRID = (-1500, -1, 0, 1000, 5000, 20000, 60000, 90000, 999999)
MERIT_GRID = (0, 1234, 10000, 47500, 120000)


def facts_for(control: str, coa: int, pct: float | None, avg_merit: Decimal | None = None) -> CollegeAidFacts:
    return CollegeAidFacts(
        control=control, state="XX", coa_in_state=None, coa_out_of_state=Decimal(coa), coa_year="2025-26",
        pct_need_met=None if pct is None else Decimal(str(pct)), avg_merit_award=avg_merit,
    )


def merit_inputs(sai: int, merit: int | None) -> EstimateInputs:
    if merit is None:
        return EstimateInputs(sai=sai, merit_mode="avg")
    return EstimateInputs(sai=sai, merit_mode="amount", merit_amount=Decimal(merit))


def assert_well_formed(est: EstimateResult, label: str) -> None:
    for name in RANGE_FIELDS:
        value = getattr(est, name)
        if value is not None:
            assert value >= 0, f"{label}: {name} negative"
            assert value % 10 == 0, f"{label}: {name} not a multiple of 10"
    if est.self_help_low is not None:
        assert est.self_help_low <= est.self_help_high, label
    if est.net_low is not None:
        assert est.net_low <= est.net_high, label
        assert est.family_low <= est.family_high, label
        assert est.family_low <= est.net_low, label
        assert est.family_high <= est.net_high, label


def test_fixture_grid_is_well_formed_and_family_never_exceeds_net():
    for case in NET_PRICE_REAL_CASES:
        _, control, coa, _, pct, _, _, _ = case
        for sai in SAI_GRID:
            for merit in (None, *MERIT_GRID):
                est = estimate_for_college(facts_for(control, coa, pct, Decimal(10000)), merit_inputs(sai, merit))
                assert_well_formed(est, f"{case[0]} sai={sai} merit={merit}")


def test_no_need_fixture_grid_is_well_formed():
    _, control, coa, _, _, _, _, _ = NO_NEED_REAL_CASE
    for sai in SAI_GRID:
        for merit in (None, *MERIT_GRID):
            est = estimate_for_college(facts_for(control, coa, None, Decimal(10000)), merit_inputs(sai, merit))
            assert_well_formed(est, f"rhodes sai={sai} merit={merit}")


def test_negative_sai_matches_zero_across_grid():
    for control in ("private", "public"):
        for merit in (None, 0, 10000):
            zero = estimate_for_college(facts_for(control, 80000, 0.8, Decimal(10000)), merit_inputs(0, merit))
            for sai in (-1500, -1000, -1):
                est = estimate_for_college(facts_for(control, 80000, 0.8, Decimal(10000)), merit_inputs(sai, merit))
                assert est == zero, f"{control} merit={merit} sai={sai}"


def test_sai_at_or_above_coa_means_no_need_for_every_coa():
    for coa in (1000, 77110, 98500):
        for sai in (coa, coa + 1, 999999):
            est = estimate_for_college(facts_for("private", coa, None, Decimal(5000)), merit_inputs(sai, 5000))
            assert est.need == 0 and est.need_package == 0, (coa, sai)
            assert est.self_help_low == est.self_help_high == 0, (coa, sai)
            assert est.net_low == est.net_high == est.family_low == est.family_high == max(0, coa - 5000)
            assert est.missing == []


def test_zero_pct_need_met_means_no_package():
    est = estimate_for_college(facts_for("private", 80000, 0.0, Decimal(10000)), merit_inputs(0, 10000))
    assert est.need_package == 0 and est.self_help_low == est.self_help_high == 0
    assert est.net_low == est.net_high == est.family_low == est.family_high == 70000
    assert est.missing == []
