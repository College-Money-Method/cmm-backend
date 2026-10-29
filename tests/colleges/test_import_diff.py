"""Import matching and upsert rules, exercised through the service on in-memory SQLite."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from src.colleges.import_diff import Flags
from src.colleges.import_service import run_import
from src.colleges.models import College, CollegeAidRecord
from tests.colleges.admin_helpers import ADMIN_ID, make_csv, row


def go(db, rows, dry_run=False, **flags):
    data = make_csv(rows)
    return run_import(db, data, Flags(**flags), dry_run, hashlib.sha256(data).hexdigest(), ADMIN_ID)


def messages(summary):
    return [w.message for w in summary.warnings]


def records(db):
    db.expire_all()
    return db.scalars(select(CollegeAidRecord)).all()


def test_blank_id_creates_then_matches_by_name_and_state(db):
    first = go(db, [row()])
    assert (first.colleges_created, first.records_created) == (1, 1)
    assert "new college" in messages(first)
    again = go(db, [row(name="alpha  COLLEGE")])
    assert (again.colleges_created, again.records_unchanged, again.records_updated) == (0, 1, 0)


def test_idempotent_reimport_is_all_unchanged(db):
    go(db, [row(), row(name="Beta U", state="CA", region="West Coast", control="public",
                      coa_in_state="30000", coa_out_of_state="50000")])
    again = go(db, [row(), row(name="Beta U", state="CA", region="West Coast", control="public",
                                coa_in_state="30000", coa_out_of_state="50000")])
    assert (again.records_unchanged, again.records_created, again.records_updated, again.colleges_updated) == (2, 0, 0, 0)


def test_unknown_college_id_is_an_error(db):
    summary = go(db, [row(college_id="11111111-1111-1111-1111-111111111111")])
    assert summary.errors[0].column == "college_id"
    assert db.scalars(select(College)).all() == []


def test_college_id_rename_warns_and_updates(db, college):
    summary = go(db, [row(college_id=str(college.id), name="Adelphi U", state="NY")])
    assert summary.errors == []
    assert any(m.startswith("name: 'Adelphi University' -> 'Adelphi U'") for m in messages(summary))
    db.expire_all()
    assert db.get(College, college.id).name == "Adelphi U"
    assert db.get(College, college.id).name_key == "adelphi u"


def test_skip_a_year_adds_new_record_and_keeps_old(db):
    go(db, [row(cds_year="2023-24")])
    summary = go(db, [row(cds_year="2025-26", release_label="2026-10")])
    assert (summary.records_created, summary.colleges_created) == (1, 0)
    assert sorted(r.cds_year for r in records(db)) == ["2023-24", "2025-26"]


def test_same_year_refresh_lists_changes(db):
    go(db, [row()])
    summary = go(db, [row(pct_need_met="0.9", release_label="2026-10")])
    assert summary.records_updated == 1
    assert any(m == "pct_need_met: 0.8000 -> 0.9000" for m in messages(summary))
    assert records(db)[0].release_label == "2026-10"


def test_empty_cell_never_overwrites_stored_value(db):
    go(db, [row()])
    summary = go(db, [row(pct_need_met="", release_label="2026-10")])
    assert "kept existing pct_need_met" in messages(summary)
    assert summary.records_unchanged == 1
    assert records(db)[0].pct_need_met == Decimal("0.8")


def test_nr_year_row_upserts_onto_same_record(db):
    go(db, [row(cds_year="")])
    summary = go(db, [row(cds_year="", pct_need_met="0.7", release_label="2026-10")])
    assert (summary.records_updated, summary.records_created) == (1, 0)
    assert len(records(db)) == 1


def test_older_release_is_error_unless_allowed(db):
    go(db, [row(release_label="2026-10")])
    blocked = go(db, [row(release_label="2026-09", pct_need_met="0.5")])
    assert blocked.errors[0].column == "release_label"
    allowed = go(db, [row(release_label="2026-09", pct_need_met="0.5")], allow_older_release=True)
    assert allowed.errors == [] and allowed.records_updated == 1


def test_state_or_control_change_needs_flag(db, college):
    changed = row(college_id=str(college.id), name=college.name, state="NJ", region="Mid-Atlantic")
    assert go(db, [changed], dry_run=True).errors[0].column == "state"
    ok = go(db, [changed], allow_identity_change=True)
    assert ok.errors == []
    db.expire_all()
    assert db.get(College, college.id).state == "NJ"
    control = row(college_id=str(college.id), name=college.name, state="NJ", region="Mid-Atlantic", control="public")
    assert go(db, [control], dry_run=True).errors[0].column == "control"


def test_manual_edit_overwrite_is_flagged(db):
    go(db, [row()])
    rec = records(db)[0]
    rec.manually_edited_at, rec.manually_edited_by = datetime.now(UTC), ADMIN_ID
    db.commit()
    summary = go(db, [row(pct_need_met="0.9", release_label="2026-10")])
    assert "overwrites manual edit: pct_need_met: 0.8000 -> 0.9000" in messages(summary)


def test_duplicate_rows_and_conflicting_ids_are_errors(db, college):
    dup = go(db, [row(), row()], dry_run=True)
    assert "duplicate" in dup.errors[0].message
    other = College(name="Other", name_key="other", state="NY", region="Mid-Atlantic", control="private")
    db.add(other)
    db.commit()
    clash = go(db, [row(college_id=str(other.id), name="Adelphi University")], dry_run=True)
    assert "different college" in clash.errors[0].message


def test_mixed_release_labels_and_rows_for_same_new_college(db):
    assert "same release_label" in go(db, [row(), row(name="B", release_label="2026-10")], dry_run=True).errors[0].message
    multi = go(db, [row(cds_year="2024-25"), row(cds_year="2025-26")])
    assert (multi.colleges_created, multi.records_created) == (1, 2)


def test_warnings_stale_year_and_unreported_year(db):
    summary = go(db, [row(cds_year="2021-22", coa_year="2021-22"), row(name="B", cds_year="2025-26"),
                      row(name="C", cds_year="")], dry_run=True)
    text = " | ".join(messages(summary))
    assert "years behind the file" in text and "cds_year not reported" in text


def test_error_cap_and_truncation(db):
    bad = [row(name=f"C{i}", pct_need_met="75") for i in range(150)]
    summary = go(db, bad, dry_run=True)
    assert len(summary.errors) == 100 and summary.truncated
