"""Latest record per college: real years beat N/R, hidden colleges excluded."""

from __future__ import annotations

from datetime import UTC, datetime

from src.colleges.latest_query import fetch_latest_records
from src.colleges.models import College, CollegeAidRecord
from src.colleges.name_key import normalize_name


def _college(db, name, hidden=False) -> College:
    row = College(
        name=name, name_key=normalize_name(name), state="NY", region="Mid-Atlantic", control="private",
        hidden_at=datetime.now(UTC) if hidden else None,
    )
    db.add(row)
    db.flush()
    return row


def _record(db, college, year, **kw):
    db.add(CollegeAidRecord(college_id=college.id, cds_year=year, release_label="2026-10", **kw))


def _latest(db) -> dict[str, str | None]:
    return {c.name: r.cds_year for c, r in fetch_latest_records(db)}


def test_newest_year_wins(db):
    c = _college(db, "Alpha")
    _record(db, c, "2024-25")
    _record(db, c, "2025-26")
    db.commit()
    assert _latest(db) == {"Alpha": "2025-26"}


def test_single_older_year_is_returned(db):
    _record(db, _college(db, "Beta"), "2024-25")
    db.commit()
    assert _latest(db) == {"Beta": "2024-25"}


def test_real_year_beats_not_reported(db):
    c = _college(db, "Gamma")
    _record(db, c, None)
    _record(db, c, "2023-24")
    db.commit()
    assert _latest(db) == {"Gamma": "2023-24"}


def test_only_not_reported_returns_null_year(db):
    _record(db, _college(db, "Delta"), None)
    db.commit()
    assert _latest(db) == {"Delta": None}


def test_hidden_and_recordless_colleges_excluded_and_sorted_by_name(db):
    _record(db, _college(db, "Zed"), "2025-26")
    _record(db, _college(db, "Hidden U", hidden=True), "2025-26")
    _college(db, "No Records")
    _record(db, _college(db, "Abe"), "2025-26")
    db.commit()
    assert [c.name for c, _ in fetch_latest_records(db)] == ["Abe", "Zed"]
