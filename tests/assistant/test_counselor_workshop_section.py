"""The school's workshop prompt section: scoping, window, cap, and what it never leaks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.assistant import counselor_workshop_section as section
from src.assistant.counselor_workshop_section import (
    CAPPED_NOTE,
    MAX_WORKSHOPS,
    NEXT_MARK,
    school_workshop_section,
)
from src.cycles.models import Cycle
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
    add_webinar(db, school.id, NOW + timedelta(days=366), name="Too Far")
    assert "(no workshops scheduled for this school in this window)" in school_workshop_section(db, school, NOW)
    add_webinar(db, school.id, NOW - timedelta(days=29), name="Recent")
    add_webinar(db, school.id, NOW + timedelta(days=200), name="Soon Enough")
    text = school_workshop_section(db, school, NOW)
    assert "Recent" in text and "Soon Enough" in text and "Too Old" not in text and "Too Far" not in text


def test_cap_limits_listed_workshops(db, school):
    for i in range(MAX_WORKSHOPS + 3):
        add_webinar(db, school.id, NOW + timedelta(days=i + 1), name=f"Night {i:02d}")
    text = school_workshop_section(db, school, NOW)
    assert text.count("\n- ") == MAX_WORKSHOPS
    assert text.endswith(CAPPED_NOTE)


def test_upcoming_listed_before_past_and_past_marked(db, school):
    add_webinar(db, school.id, NOW - timedelta(days=3), name="Earlier")
    add_webinar(db, school.id, NOW + timedelta(days=3), name="Later")
    text = school_workshop_section(db, school, NOW)
    assert text.index("Later") < text.index("Earlier")
    assert "(already held)" in text.split("Earlier")[1]
    assert "(already held)" not in text.split("Earlier")[0]
    assert text.count(NEXT_MARK) == 1 and NEXT_MARK in text.split("Earlier")[0]


def test_no_next_mark_when_only_past_workshops(db, school):
    add_webinar(db, school.id, NOW - timedelta(days=3), name="Earlier")
    assert NEXT_MARK not in school_workshop_section(db, school, NOW)


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
    _current_cycle(db, (NOW - timedelta(days=MAX_WORKSHOPS + 10)).date())
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


def test_upcoming_workshop_shows_only_the_session_total(db, school):
    other = School(id=uuid.uuid4(), name="B High", slug="b-high")
    db.add(other)
    db.commit()
    webinar = add_webinar(db, school.id, NOW + timedelta(days=3), name="Shared Night")
    db.add(section.PortalMapping(school_id=other.id, webinar_id=webinar.id))
    _register(db, webinar, school.id, n=2)
    _register(db, webinar, other.id, n=3)
    _register(db, webinar, None)  # no school on record still counts toward the total
    text = school_workshop_section(db, school, NOW)
    assert "total registrations for this workshop: 6" in text
    assert "attended" not in text
    # The total only: never the school's own share, who registered, or the other school.
    assert "your school" not in text
    assert "Pat" not in text and "Parent" not in text and "@example.com" not in text and "B High" not in text


def test_held_workshop_adds_attendance(db, school):
    other = School(id=uuid.uuid4(), name="B High", slug="b-high")
    db.add(other)
    webinar = add_webinar(db, school.id, NOW - timedelta(days=3))
    _register(db, webinar, school.id, attended=True, n=2)
    _register(db, webinar, school.id)
    _register(db, webinar, other.id, attended=True)
    text = school_workshop_section(db, school, NOW)
    assert "total registrations for this workshop: 4; total attended: 3" in text
    assert "your school" not in text


def test_workshop_with_no_registrations_shows_zeros(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3))
    assert "total registrations for this workshop: 0" in school_workshop_section(db, school, NOW)


def _current_cycle(db, began):
    db.add(Cycle(id=uuid.uuid4(), name="2026-27", beginning_date=began, is_current=True))
    db.commit()


def test_past_window_reaches_back_to_the_current_cycle_start(db, school):
    _current_cycle(db, (NOW - timedelta(days=70)).date())
    add_webinar(db, school.id, NOW - timedelta(days=60), name="September Night")
    add_webinar(db, school.id, NOW - timedelta(days=80), name="Last Cycle Night")
    text = school_workshop_section(db, school, NOW)
    assert "September Night" in text and "Last Cycle Night" not in text


def test_past_window_is_at_least_thirty_days_and_at_most_a_year(db, school):
    _current_cycle(db, (NOW - timedelta(days=5)).date())
    assert section.past_window_start(db, NOW) == NOW - timedelta(days=section.PAST_DAYS)
    db.query(Cycle).update({Cycle.beginning_date: (NOW - timedelta(days=900)).date()})
    db.commit()
    assert section.past_window_start(db, NOW) == NOW - timedelta(days=section.MAX_PAST_DAYS)


def test_ahead_window_runs_to_the_cycle_end_within_bounds(db, school):
    db.add(Cycle(id=uuid.uuid4(), name="2026-27", end_date=(NOW + timedelta(days=200)).date(), is_current=True))
    db.commit()
    add_webinar(db, school.id, NOW + timedelta(days=190), name="Spring Night")
    add_webinar(db, school.id, NOW + timedelta(days=230), name="Next Cycle Night")
    text = school_workshop_section(db, school, NOW)
    assert "Spring Night" in text and "Next Cycle Night" not in text
    # Near a cycle's end the next months still show.
    db.query(Cycle).update({Cycle.end_date: (NOW + timedelta(days=5)).date()})
    db.commit()
    assert section.ahead_window_end(db, NOW) == NOW + timedelta(days=section.AHEAD_DAYS)


def test_summary_counts_held_and_upcoming_and_names_the_most_registered(db, school):
    popular = add_webinar(db, school.id, NOW - timedelta(days=3), name="Popular Night")
    quiet = add_webinar(db, school.id, NOW + timedelta(days=3), name="Quiet Night")
    add_webinar(db, school.id, NOW + timedelta(days=9), name="Later Night")
    _register(db, popular, school.id, n=5)
    _register(db, quiet, school.id, n=2)
    summary = school_workshop_section(db, school, NOW).splitlines()[1]
    assert summary.startswith("Summary: 1 held so far this cycle and 2 upcoming are listed below.")
    assert "Most registered: Popular Night" in summary and "(total registrations for this workshop: 5)" in summary


def test_summary_names_no_top_workshop_without_registrations(db, school):
    add_webinar(db, school.id, NOW + timedelta(days=3))
    assert "Most registered" not in school_workshop_section(db, school, NOW)
