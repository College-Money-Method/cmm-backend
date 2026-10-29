"""Derived metrics: nulls, divide-by-zero, 4 dp rounding, aid_form table."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.colleges.derived_metrics import (
    merit_pct_of_coa,
    normalize_aid_form,
    pct_need_unmet,
    pct_non_need_paying_full_coa,
)

D = Decimal


def test_pct_need_unmet():
    assert pct_need_unmet(D("0.8250")) == D("0.1750")
    assert pct_need_unmet(D("1")) == D("0.0000")
    assert pct_need_unmet(None) is None


def test_paying_full_coa_is_one_minus_merit_share():
    assert pct_non_need_paying_full_coa(D("0.3000")) == D("0.7000")
    assert pct_non_need_paying_full_coa(D("0")) == D("1.0000")
    assert pct_non_need_paying_full_coa(None) is None


def test_merit_pct_of_coa_rounds_to_four_places():
    assert merit_pct_of_coa(D("10000"), D("80000")) == D("0.1250")
    assert merit_pct_of_coa(D("1"), D("3")) == D("0.3333")
    assert merit_pct_of_coa(D("2"), D("3")) == D("0.6667")


def test_merit_pct_of_coa_null_inputs_and_zero_divisor():
    assert merit_pct_of_coa(None, D("80000")) is None
    assert merit_pct_of_coa(D("1000"), None) is None
    assert merit_pct_of_coa(D("1000"), D("0")) is None
    assert merit_pct_of_coa(D("0"), D("80000")) == D("0.0000")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None), ("", None), ("   ", None),
        ("FAFSA", "fafsa"), ("fafsa only", "fafsa"),
        ("CSS Profile", "css"), ("FAFSA and CSS Profile", "css"),
        ("Princeton Form", "institutional"), ("Institutional application", "institutional"),
        ("None required", "other"),
    ],
)
def test_normalize_aid_form(raw, expected):
    assert normalize_aid_form(raw) == expected
