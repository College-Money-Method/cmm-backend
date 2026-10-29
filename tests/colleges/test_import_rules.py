"""Value rules shared by the CSV import and the edit forms."""

from decimal import Decimal

import pytest

from src.colleges import import_rules as r
from src.colleges.import_row import parse_row
from src.colleges.models import REGIONS
from tests.colleges.admin_helpers import row


def test_empty_and_nr_cells_read_as_none():
    for raw in ("", "  ", "N/R", "nr", None):
        assert r.parse_money(raw) is None
        assert r.parse_pct(raw) is None
        assert r.parse_year(raw) is None


def test_money_strips_symbols_and_quantizes():
    assert r.parse_money("$38,357") == Decimal("38357.00")
    assert r.parse_money("1.005") == Decimal("1.01")
    for bad in ("-5", "abc", "NaN", "1e12"):
        with pytest.raises(r.RuleError):
            r.parse_money(bad)


def test_percent_is_a_fraction_quantized_to_four_decimals():
    assert r.parse_pct("0.123456") == Decimal("0.1235")
    assert r.parse_pct("1") == Decimal("1.0000")
    for bad in ("75", "85%", "1.5", "-0.1"):
        with pytest.raises(r.RuleError):
            r.parse_pct(bad)


def test_count_must_be_whole_non_negative():
    assert r.parse_count("1,234") == 1234
    assert r.parse_count("12.0") == 12
    for bad in ("1.5", "-1", "x"):
        with pytest.raises(r.RuleError):
            r.parse_count(bad)


def test_years_consecutive_in_range_and_excel_dates_rejected():
    assert r.parse_year("2025-26") == "2025-26"
    assert r.parse_year("2025-2026") == "2025-26"
    assert r.parse_year("2009-10") == "2009-10"
    for bad in ("2025-27", "2062-63", "1999-00", "2025-09-01", "Sep-25", "45902", "2025/26", "2025-09"):
        with pytest.raises(r.RuleError):
            r.parse_year(bad)


def test_release_label_format():
    assert r.parse_release("2026-09") == "2026-09"
    for bad in ("", "2026-13", "26-09", "2026-9"):
        with pytest.raises(r.RuleError):
            r.parse_release(bad)


def test_state_control_bool():
    assert r.parse_state("ny") == "NY"
    assert r.parse_control("Public") == "public"
    assert r.parse_bool("TRUE") is True and r.parse_bool("false") is False and r.parse_bool("") is None
    for fn, bad in ((r.parse_state, "ZZ"), (r.parse_control, "charter"), (r.parse_bool, "yes")):
        with pytest.raises(r.RuleError):
            fn(bad)


def test_region_map_covers_states_and_regions():
    assert len(r.REGION_BY_STATE) >= 51  # 50 states + DC (+ PR)
    assert set(r.REGION_BY_STATE.values()) == set(REGIONS)
    assert r.REGION_BY_STATE["DC"] == "Mid-Atlantic"


def test_echo_is_capped():
    assert len(r.echo("x" * 500)) < 60


def test_valid_row_parses_clean():
    parsed = parse_row(row())
    assert parsed.errors == []
    assert parsed.values["pct_need_met"] == Decimal("0.8000")
    assert parsed.values["region"] == "Mid-Atlantic"
    assert parsed.values["college_id"] is None


def test_row_errors_are_per_column():
    parsed = parse_row(row(pct_need_met="75", state="NY", region="Plains", cds_year="2025-27", college_id="nope"))
    assert {c for c, _ in parsed.errors} == {"pct_need_met", "region", "cds_year", "college_id"}


def test_coa_requires_coa_year():
    parsed = parse_row(row(coa_year=""))
    assert [c for c, _ in parsed.errors] == ["coa_year"]
    assert parse_row(row(coa_year="", coa_out_of_state="")).errors == []


def test_formula_and_outlier_warnings():
    parsed = parse_row(row(name="=cmd", pct_need_met="0.05", pct_non_need_receiving_merit="1", avg_merit_award="0"))
    text = " | ".join(parsed.warnings)
    assert "formula" in text and "very low" in text and "average award is 0" in text
    assert any("no non-need" in w for w in parse_row(row(pct_non_need_receiving_merit="0")).warnings)
    assert any("60%" in w for w in parse_row(row(avg_merit_award="50000")).warnings)
    assert any("differs from CDS year" in w for w in parse_row(row(coa_year="2024-25")).warnings)
