"""Schema constraints the import and the read path rely on."""

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from src.colleges.models import College, CollegeAidRecord


def _record(college, cds_year="2025-26", **kw):
    return CollegeAidRecord(college_id=college.id, cds_year=cds_year, release_label="2026-09", **kw)


def test_round_trip_keeps_decimals_and_null(db, college):
    db.add(_record(college, pct_need_met=Decimal("0.5500"), coa_out_of_state=Decimal("70000.00")))
    db.commit()
    row = db.query(CollegeAidRecord).one()
    assert row.pct_need_met == Decimal("0.55")
    assert row.pct_non_need_receiving_merit is None
    assert row.coa_estimated is False
    assert college.records[0].id == row.id


def test_duplicate_college_year_rejected(db, college):
    db.add(_record(college))
    db.commit()
    db.add(_record(college))
    with pytest.raises(IntegrityError):
        db.commit()


def test_second_null_year_row_rejected(db, college):
    db.add(_record(college, cds_year=None))
    db.commit()
    db.add(_record(college, cds_year=None))
    with pytest.raises(IntegrityError):
        db.commit()


def test_null_year_row_coexists_with_real_year(db, college):
    db.add_all([_record(college, cds_year=None), _record(college, cds_year="2025-26")])
    db.commit()
    assert db.query(CollegeAidRecord).count() == 2


@pytest.mark.parametrize("field", ["pct_need_met", "pct_non_need_receiving_merit"])
@pytest.mark.parametrize("value", [Decimal("1.2"), Decimal("-0.1")])
def test_pct_out_of_range_rejected(db, college, field, value):
    db.add(_record(college, **{field: value}))
    with pytest.raises(IntegrityError):
        db.commit()


def test_negative_money_rejected(db, college):
    db.add(_record(college, avg_merit_award=Decimal(-1)))
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize("year", ["2025", "2025-2026"])
def test_bad_year_length_rejected(db, college, year):
    db.add(_record(college, cds_year=year))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_name_key_and_state_rejected(db, college):
    db.add(College(name="ADELPHI UNIVERSITY", name_key=college.name_key, state="NY",
                   region="Mid-Atlantic", control="private"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_name_key_other_state_allowed(db, college):
    db.add(College(name="Adelphi University", name_key=college.name_key, state="NJ",
                   region="Mid-Atlantic", control="private"))
    db.commit()
    assert db.query(College).count() == 2


@pytest.mark.parametrize("field,value", [("control", "charter"), ("region", "Mars"), ("state", "NYC")])
def test_college_enum_checks(db, field, value):
    kw = {"name": "X", "name_key": "x", "state": "NY", "region": "Plains", "control": "public"} | {field: value}
    db.add(College(**kw))
    with pytest.raises(IntegrityError):
        db.commit()
