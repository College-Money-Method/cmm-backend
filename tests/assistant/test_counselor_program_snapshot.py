"""The school's program reach section: figures as on the hub's reach card, never inflated."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from src.assistant import counselor_program_snapshot as snapshot
from src.assistant.counselor_program_snapshot import (
    SECTION_HEADER,
    program_snapshot_section,
)
from src.schools.models import School
from src.workshops.models import WorkshopRegistration
from tests.assistant.counselor_test_support import add_webinar, make_counselor_factory

SCHOOL = School(id=uuid.uuid4(), name="A High")
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def _section(monkeypatch, reach: dict) -> str:
    monkeypatch.setattr(snapshot, "get_reach_data", lambda db, school_id: reach)
    return program_snapshot_section(None, SCHOOL)


def test_reports_registrants_enrollment_and_reach(monkeypatch):
    text = _section(monkeypatch, {"distinct_registrants": 42, "enrollment": 1200, "reach_pct": 3.5, "enrollment_range": None})
    assert text.startswith(SECTION_HEADER)
    assert "(distinct registration emails): 42" in text
    assert "enrollment on file: 1200" in text and "that enrollment): 3.5%" in text


def test_zero_registrants_is_stated_not_hidden(monkeypatch):
    text = _section(monkeypatch, {"distinct_registrants": 0, "enrollment": 800, "reach_pct": 0.0, "enrollment_range": None})
    assert "emails): 0" in text and "): 0%" in text


def test_missing_enrollment_says_reach_is_unknown(monkeypatch):
    text = _section(monkeypatch, {"distinct_registrants": 9, "enrollment": None, "reach_pct": None, "enrollment_range": None})
    assert "reach % is unknown" in text and "Reach (" not in text


def test_counts_distinct_families_of_the_given_school_only():
    with make_counselor_factory()() as db:
        ours = School(id=uuid.uuid4(), name="A High", slug="a-high", enrollment_9_12=200)
        other = School(id=uuid.uuid4(), name="B High", slug="b-high", enrollment_9_12=100)
        db.add_all([ours, other])
        db.commit()
        first, second = add_webinar(db, ours.id, NOW), add_webinar(db, ours.id, NOW)
        # The same family at two sessions counts once; another school's family not at all.
        db.add_all([
            WorkshopRegistration(webinar_id=first.id, school_id=ours.id, email="a@example.com"),
            WorkshopRegistration(webinar_id=second.id, school_id=ours.id, email="a@example.com"),
            WorkshopRegistration(webinar_id=first.id, school_id=ours.id, email="b@example.com"),
            WorkshopRegistration(webinar_id=first.id, school_id=other.id, email="c@example.com"),
        ])
        db.commit()
        text = program_snapshot_section(db, ours)
        assert "emails): 2" in text and "that enrollment): 1%" in text
