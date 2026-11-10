"""The school's workshop prompt section: scoping, window, cap, and what it never leaks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.assistant import counselor_workshop_section as section
from src.assistant.counselor_workshop_section import (
    MAX_WORKSHOPS,
    school_workshop_section,
)
from src.schools.models import School
from src.workshops.models import WorkshopRegistration
from tests.assistant.counselor_test_support import add_webinar, make_counselor_factory

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
ZOOM = "https://zoom.example/j/1"


@pytest.fixture
def db():
    with make_counselor_factory()() as session:
        yield session


@pytest.fixture
def school(db):
    row = School(id=uuid.uuid4(), name="A High", slug="a-high", state="TX", cmm_website_password="secret-pw-xyz")
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def origin(monkeypatch):
    monkeypatch.setattr(section, "email_origin", lambda: "https://cmm.example")


def test_other_schools_webinar_is_excluded(db, school):
    other = School(id=uuid.uuid4(), name="B High", slug="b-high")
    db.add(other)
    db.commit()
    add_webinar(db, other.id, NOW + timedelta(days=5), name="Other School Night")
    add_webinar(db, school.id, NOW + timedelta(days=6), name="Our Night")
    text = school_workshop_section(db, school, NOW)
    assert "Our Night" in text and "Other School Night" not in text


def test_window_bounds_and_empty_message(db, school):
    add_webinar(db, school.id, NOW - timedelta(days=31), name="Too Old")
    add_webinar(db, school.id, NOW + timedelta(days=121), name="Too Far")
    assert "(no workshops scheduled for this school in this window)" in school_workshop_section(db, school, NOW)
    add_webinar(db, school.id, NOW - timedelta(days=29), name="Recent")
    add_webinar(db, school.id, NOW + timedelta(days=119), name="Soon Enough")
    text = school_workshop_section(db, school, NOW)
    assert "Recent" in text and "Soon Enough" in text and "Too Old" not in text and "Too Far" not in text


def test_cap_is_twelve(db, school):
    for i in range(MAX_WORKSHOPS + 3):
        add_webinar(db, school.id, NOW + timedelta(days=i + 1), name=f"Night {i:02d}")
    text = school_workshop_section(db, school, NOW)
    assert text.count("\n- ") == MAX_WORKSHOPS


def test_upcoming_listed_before_past_and_past_marked(db, school):
    add_webinar(db, school.id, NOW - timedelta(days=3), name="Earlier")
    add_webinar(db, school.id, NOW + timedelta(days=3), name="Later")
    text = school_workshop_section(db, school, NOW)
    assert text.index("Later") < text.index("Earlier")
    assert "(already held)" in text.split("Earlier")[1]
    assert "(already held)" not in text.split("Earlier")[0]


def test_school_override_grades_win(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3), grades="12", override={"suggested_grades": "9"})
    assert "9th" in school_workshop_section(db, school, NOW)


def test_password_never_appears(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3))
    assert "secret-pw-xyz" not in school_workshop_section(db, school, NOW)


def test_link_uses_school_slug_when_origin_set(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3))
    assert "https://cmm.example/school/a-high/workshops/" in school_workshop_section(db, school, NOW)


def test_link_falls_back_to_zoom_without_origin(db, school, monkeypatch):
    monkeypatch.setattr(section, "email_origin", lambda: "")
    add_webinar(db, school.id, NOW + timedelta(days=3))
    text = school_workshop_section(db, school, NOW)
    assert ZOOM in text and "/school/a-high" not in text


def test_upcoming_workshop_survives_a_full_past_month(db, school):
    for i in range(MAX_WORKSHOPS + 2):
        add_webinar(db, school.id, NOW - timedelta(days=i + 1), name=f"Past {i:02d}")
    add_webinar(db, school.id, NOW + timedelta(days=60), name="Next Up")
    text = school_workshop_section(db, school, NOW)
    assert text.count("\n- ") == MAX_WORKSHOPS
    assert "Next Up" in text and "Past 00" in text and f"Past {MAX_WORKSHOPS + 1:02d}" not in text


def _register(db, webinar, school_id, *, attended=False, n=1):
    db.add_all(
        WorkshopRegistration(
            webinar_id=webinar.id, school_id=school_id, email=f"p{uuid.uuid4().hex[:8]}@example.com",
            first_name="Pat", last_name="Parent", attended=attended,
        )
        for _ in range(n)
    )
    db.commit()


def test_upcoming_workshop_shows_total_and_school_registrations(db, school):
    other = School(id=uuid.uuid4(), name="B High", slug="b-high")
    db.add(other)
    db.commit()
    webinar = add_webinar(db, school.id, NOW + timedelta(days=3), name="Shared Night")
    db.add(section.PortalMapping(school_id=other.id, webinar_id=webinar.id))
    _register(db, webinar, school.id, n=2)
    _register(db, webinar, other.id, n=3)
    _register(db, webinar, None)  # no school on record still counts toward the total
    text = school_workshop_section(db, school, NOW)
    assert "registered: 6 total, 2 from your school" in text
    assert "attended" not in text
    # Counts only: never who registered, never the other school.
    assert "Pat" not in text and "Parent" not in text and "@example.com" not in text and "B High" not in text


def test_held_workshop_adds_attendance(db, school):
    other = School(id=uuid.uuid4(), name="B High", slug="b-high")
    db.add(other)
    webinar = add_webinar(db, school.id, NOW - timedelta(days=3))
    _register(db, webinar, school.id, attended=True, n=2)
    _register(db, webinar, school.id)
    _register(db, webinar, other.id, attended=True)
    text = school_workshop_section(db, school, NOW)
    assert "registered: 4 total, 3 from your school; attended: 3 total, 2 from your school" in text


def test_workshop_with_no_registrations_shows_zeros(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3))
    assert "registered: 0 total, 0 from your school" in school_workshop_section(db, school, NOW)
