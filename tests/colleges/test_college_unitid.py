"""Federal IPEDS UNITID: rule, optional CSV column, import matching, admin edits, export."""

from __future__ import annotations

import csv
import hashlib
import io

import pytest
from sqlalchemy import select

from src.colleges.import_diff import Flags
from src.colleges.import_rules import COLUMNS, RuleError, parse_unitid
from src.colleges.import_service import run_import
from src.colleges.models import College
from tests.colleges.admin_helpers import ADMIN_ID, BASE_URL, make_csv, row

WITH_UNITID = (*COLUMNS, "unitid")


def go(db, rows, columns=WITH_UNITID, dry_run=False, **flags):
    data = make_csv(rows, columns)
    return run_import(db, data, Flags(**flags), dry_run, hashlib.sha256(data).hexdigest(), ADMIN_ID)


def colleges(db):
    db.expire_all()
    return db.scalars(select(College).order_by(College.name)).all()


@pytest.mark.parametrize("value,expected", [("", None), (" N/R ", None), (None, None), ("190150", 190150), (190150, 190150)])
def test_parse_unitid_accepts(value, expected):
    assert parse_unitid(value) == expected


@pytest.mark.parametrize("value", ["12345", "1234567", "0123456", "012345", "19015a", "190150.0", "-190150", "19015²", "１９０１５０", True])
def test_parse_unitid_rejects(value):
    with pytest.raises(RuleError, match="6-digit"):
        parse_unitid(value)


def test_unitid_column_is_optional_and_unknown_columns_still_fail(db):
    assert go(db, [row()], columns=COLUMNS).errors == []
    assert colleges(db)[0].unitid is None
    bad = go(db, [row()], columns=(*COLUMNS, "ipeds"), dry_run=True)
    assert "header must match" in bad.errors[0].message


def test_new_college_gets_unitid_and_blank_keeps_it(db):
    assert go(db, [row(unitid="190150")]).errors == []
    assert colleges(db)[0].unitid == 190150
    again = go(db, [row(unitid="")])
    assert (again.colleges_updated, again.records_unchanged) == (0, 1)
    assert colleges(db)[0].unitid == 190150


def test_unitid_fills_existing_college_matched_by_name(db, college):
    summary = go(db, [row(name="Adelphi University", unitid="188429")])
    assert summary.errors == [] and summary.colleges_updated == 1
    assert "unitid: empty -> 188429" in [w.message for w in summary.warnings]
    assert colleges(db)[0].unitid == 188429


def test_unitid_matches_before_name_and_state(db, college):
    college.unitid = 188429
    db.commit()
    summary = go(db, [row(name="Adelphi U", unitid="188429")])
    assert summary.errors == [] and summary.colleges_created == 0
    assert [c.name for c in colleges(db)] == ["Adelphi U"]


def test_changing_a_stored_unitid_needs_flag(db, college):
    college.unitid = 188429
    db.commit()
    changed = row(college_id=str(college.id), name=college.name, unitid="188430")
    assert go(db, [changed], dry_run=True).errors[0].column == "unitid"
    assert go(db, [changed], allow_identity_change=True).errors == []
    assert colleges(db)[0].unitid == 188430


def test_unitid_owned_by_another_college_or_shared_in_file_is_error(db, college):
    college.unitid = 188429
    other = College(name="Beta U", name_key="beta u", state="CA", region="West Coast", control="private")
    db.add(other)
    db.commit()
    by_id = go(db, [row(college_id=str(other.id), name="Beta U", state="CA", region="West Coast",
                        unitid="188429")], dry_run=True)
    assert "UNITID 188429 already belongs" in by_id.errors[0].message
    shared = go(db, [row(name="Gamma", unitid="111111"), row(name="Delta", unitid="111111")], dry_run=True)
    assert "UNITID 111111 already belongs" in shared.errors[0].message
    disagree = go(db, [row(name="Gamma", unitid="111111"), row(name="Gamma", unitid="222222", cds_year="2024-25")],
                  dry_run=True)
    assert "disagree" in disagree.errors[0].message


def test_create_and_patch_college_unitid(client, db):
    made = client.post(BASE_URL, json={"name": "Gamma", "state": "CA", "control": "public", "unitid": "110635"})
    assert made.status_code == 201 and made.json()["unitid"] == 110635
    assert client.post(BASE_URL, json={"name": "Delta", "state": "CA", "control": "public", "unitid": "12"}).status_code == 422
    clash = client.post(BASE_URL, json={"name": "Delta", "state": "CA", "control": "public", "unitid": 110635})
    assert clash.status_code == 409 and "Gamma (CA)" in clash.json()["detail"]
    cid = made.json()["college_id"]
    assert client.patch(f"{BASE_URL}/{cid}", json={"unitid": 110644}).json()["unitid"] == 110644
    assert client.patch(f"{BASE_URL}/{cid}", json={"unitid": None}).json()["unitid"] is None
    assert client.patch(f"{BASE_URL}/{cid}", json={"unitid": "abc"}).status_code == 422


def test_csv_export_carries_unitid(client, db):
    client.post(BASE_URL, json={"name": "Gamma", "state": "CA", "control": "public", "unitid": "110635"})
    client.post(BASE_URL, json={"name": "Delta", "state": "CA", "control": "public"})
    parsed = list(csv.DictReader(io.StringIO(client.get(BASE_URL, params={"format": "csv"}).text)))
    assert {r["name"]: r["unitid"] for r in parsed} == {"Gamma": "110635", "Delta": ""}
