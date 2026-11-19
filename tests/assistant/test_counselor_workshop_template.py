"""A workshop email starts from that workshop's own Hub template, chosen in code."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.assistant.counselor_workshop_section import school_webinar_rows, workshop_values
from src.assistant.counselor_workshop_template import (
    SECTION_HEADER,
    match_webinar,
    workshop_template_section,
)
from src.schools.models import School
from src.workshops.models import WorkshopEmailTemplate
from tests.assistant.counselor_test_support import add_webinar, make_counselor_factory

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
ORIGIN = "https://cmm.example"


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


def _workshop(db, school, days, name, number):
    webinar = add_webinar(db, school.id, NOW + timedelta(days=days), name=name)
    webinar.workshop.sequence_number = number
    db.commit()
    return webinar


@pytest.fixture
def webinars(db, school):
    """Workshop #1 already held, #3 next, #5 later."""
    return {
        1: _workshop(db, school, -10, "Paying for College 101", 1),
        3: _workshop(db, school, 5, "FAFSA Live! with CMM", 3),
        5: _workshop(db, school, 30, "Merit Aid: Where to Look", 5),
    }


def _template(db, webinar, kind, subject, body):
    db.add(WorkshopEmailTemplate(
        id=uuid.uuid4(), workshop_id=webinar.workshop_id, type=kind, name=f"{kind} {webinar.workshop.name}",
        subject=subject, body=body,
    ))
    db.commit()


def _match(db, school, message):
    row = match_webinar(message, *school_webinar_rows(db, school, NOW))
    return row[0].workshop.sequence_number if row else None


def _section(db, school, message, card_type=None):
    return workshop_template_section(db, school, message, card_type, {"school_name": "A High"}, ORIGIN, NOW)


@pytest.mark.parametrize(("message", "number"), [
    ("Draft an invite for workshop #3", 3),
    ("Write the email for Workshop 5", 5),
    ("an announcement for #1 please", 1),
    ("Can you write an email for FAFSA Live?", 3),
    ("email families about merit aid", 5),
    ("Promote our next workshop", 3),
    ("an invite for the upcoming session", 3),
])
def test_matches_the_workshop_the_message_names(db, school, webinars, message, number):
    assert _match(db, school, message) == number


@pytest.mark.parametrize("message", [
    "An email covering #3 and #5",
    "Write an email about next steps for seniors",
    "Write a newsletter blurb",
])
def test_unclear_or_missing_workshop_matches_nothing(db, school, webinars, message):
    assert _match(db, school, message) is None


def test_uses_the_matched_workshops_own_announcement_filled_in(db, school, webinars):
    _template(db, webinars[1], "announcement", "Workshop #1 is coming", "Generic body from #1")
    _template(db, webinars[3], "announcement", "Join {{workshop_name}} on {{date}}",
              "Hello from {{school_name}}. Register: {{registration_link}}\nPassword: {{resource_center_password}}")
    _template(db, webinars[3], "followup", "Thanks for joining", "Recap body")
    text = _section(db, school, "Write an invite for FAFSA Live")
    values = workshop_values(school, webinars[3], webinars[3].portal_mappings[0], ORIGIN)
    assert text.startswith(SECTION_HEADER)
    assert "announcement (invite) template for FAFSA Live! with CMM" in text
    assert f"Subject: Join FAFSA Live! with CMM on {values['date']}" in text
    assert f"Hello from A High. Register: {values['registration_link']}" in text
    assert "{{resource_center_password}}" in text and "secret-pw-xyz" not in text
    assert "#1" not in text and "Recap body" not in text


def test_follow_up_request_uses_the_follow_up_template(db, school, webinars):
    _template(db, webinars[1], "announcement", "Invite", "Invite body")
    _template(db, webinars[1], "followup", "Thanks", "Thanks for attending {{workshop_name}}")
    text = _section(db, school, "Write the follow-up email for workshop #1")
    assert "follow-up template for Paying for College 101" in text
    assert "Thanks for attending Paying for College 101" in text and "Invite body" not in text


def test_workshop_without_its_own_template_says_so(db, school, webinars):
    _template(db, webinars[1], "announcement", "Workshop #1 is coming", "Generic body from #1")
    text = _section(db, school, "Promote our next workshop", card_type="workshop_promotion")
    assert "FAFSA Live! with CMM has no announcement (invite) template in the Hub" in text
    assert "do not start from another workshop's template" in text
    assert "Generic body from #1" not in text


def test_promotion_card_needs_no_email_words(db, school, webinars):
    _template(db, webinars[5], "announcement", "Merit", "Merit body")
    assert "Merit body" in _section(db, school, "Merit Aid", card_type="workshop_promotion")


def test_no_section_without_an_email_request_or_a_match(db, school, webinars):
    _template(db, webinars[3], "announcement", "Invite", "Invite body")
    assert _section(db, school, "How many families registered for workshop #3?") == ""
    assert _section(db, school, "Write an email about next steps") == ""
    assert _section(db, school, "") == ""
