"""Workshop {{date}}/{{time}} render in the school's timezone, not stored UTC.

`Webinar.start_datetime` is stored UTC. For any US evening workshop that is
already the *next day* in UTC, so rendering the stored value straight out gets
the date wrong, not just the hour — which is what these tests pin down.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.config import settings
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.email_template_models import EmailTemplate
from src.emails.workshop_merge_tags import build_workshop_merge_replacements
from src.schools.display_timezone import FALLBACK_TIMEZONE, resolve_display_timezone
from src.schools.models import Contact, School
from src.schools.schemas import SchoolUpdate
from src.workshops.models import PortalMapping, Webinar, Workshop

# 7:00 PM Eastern on April 2 — stored as 23:00 UTC the same day under EDT.
EVENING_ET = datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc)
# 9:00 PM Pacific on April 2 — stored as 04:00 UTC on April *3*.
LATE_PT = datetime(2026, 4, 3, 4, 0, tzinfo=timezone.utc)


def _tags(start: datetime | None, display_timezone: str | None) -> dict[str, str]:
    return build_workshop_merge_replacements(
        school_name="Test High",
        family_label="Test High families",
        counselor_name="Casey Counselor",
        school_slug="test-high",
        workshop_name="FAFSA Basics",
        webinar_id=uuid.uuid4(),
        start_datetime=start,
        suggested_grades="11,12",
        cycle_name="2025-26",
        display_timezone=display_timezone,
    )


# ── Zone resolution ─────────────────────────────────────────────────────────


def test_school_timezone_wins_over_the_app_default(monkeypatch):
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/New_York")
    assert str(resolve_display_timezone("America/Denver")) == "America/Denver"


def test_no_school_timezone_uses_the_app_default(monkeypatch):
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/Chicago")
    assert str(resolve_display_timezone(None)) == "America/Chicago"
    assert str(resolve_display_timezone("")) == "America/Chicago"


def test_an_unloadable_configured_default_degrades_instead_of_raising(monkeypatch):
    """A typo in the env must not take down every workshop send."""
    monkeypatch.setattr(settings, "workshop_display_timezone", "Not/AZone")
    assert str(resolve_display_timezone(None)) == FALLBACK_TIMEZONE
    assert str(resolve_display_timezone("Also/Bogus")) == FALLBACK_TIMEZONE


# ── Rendering ───────────────────────────────────────────────────────────────


def test_evening_workshop_keeps_its_own_date_and_local_hour():
    tags = _tags(EVENING_ET, "America/New_York")

    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "7:00 PM EDT"


def test_utc_rollover_does_not_advertise_the_workshop_a_day_late():
    """The regression this whole feature exists for: 9 PM Pacific is stored as
    04:00 UTC the NEXT day, so the untranslated render named the wrong day."""
    # What the stored value says on its own, and must NOT be what goes out.
    assert LATE_PT.strftime("%A, %B") == "Friday, April" and LATE_PT.day == 3

    tags = _tags(LATE_PT, "America/Los_Angeles")
    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "9:00 PM PDT"


def test_same_instant_reads_differently_per_school_timezone():
    assert _tags(LATE_PT, "America/Los_Angeles")["time"] == "9:00 PM PDT"
    assert _tags(LATE_PT, "America/New_York")["time"] == "12:00 AM EDT"
    assert _tags(LATE_PT, "Pacific/Honolulu")["time"] == "6:00 PM HST"


def test_a_school_with_no_timezone_falls_back_to_the_app_default(monkeypatch):
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/Chicago")
    assert _tags(EVENING_ET, None)["time"] == "6:00 PM CDT"


def test_a_naive_datetime_is_left_where_it_is():
    """No offset to convert from — shifting it would corrupt a correct value."""
    tags = _tags(datetime(2026, 4, 2, 19, 0), "America/Los_Angeles")

    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "7:00 PM"


def test_missing_start_datetime_still_reads_tbd():
    tags = _tags(None, "America/New_York")
    assert tags["date"] == "TBD"
    assert tags["time"] == "TBD"


# ── Write path ──────────────────────────────────────────────────────────────


def test_an_unsupported_timezone_is_rejected_before_it_can_be_stored():
    with pytest.raises(ValidationError):
        SchoolUpdate(display_timezone="Mars/Olympus")


def test_a_blank_timezone_clears_the_override():
    """Empty must become NULL (app-wide default), never a string that fails to
    load as a zone at send time."""
    assert SchoolUpdate(display_timezone="   ").display_timezone is None


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_the_automation_runner_passes_the_schools_own_timezone(scheduler_sessionmaker, monkeypatch):
    """The formatter is unit-tested above; what this pins is that the send path
    actually hands it the school's zone.

    Asserted on the call rather than on the rendered HTML because the SQLite
    test DB returns naive datetimes — the conversion is a no-op there whatever
    zone is passed, so rendered output could not tell a wired-up call from a
    dropped one.
    """
    from src.emails import automation_runner

    session = scheduler_sessionmaker()
    template_id = uuid.uuid4()
    session.add(
        EmailTemplate(
            id=template_id,
            category="workshop",
            name="Reminder",
            subject="{{workshop_name}}",
            body_json='{"type":"doc","content":[{"type":"paragraph","content":[{"type":"mergeTag","attrs":{"tag":"date"}}]}]}',
        )
    )
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (
        uuid.uuid4() for _ in range(6)
    )
    session.add(
        School(
            id=school_id,
            name="Westside High",
            slug=f"s-{school_id.hex[:8]}",
            is_current_customer=True,
            display_timezone="America/Los_Angeles",
        )
    )
    session.add(
        Contact(id=contact_id, school_id=school_id, email="family@example.com", role="hub_user", auto_emails=True)
    )
    session.add(Workshop(id=workshop_id, name="FAFSA Basics"))
    session.add(
        Webinar(
            id=webinar_id,
            workshop_id=workshop_id,
            start_datetime=datetime.now(timezone.utc) - timedelta(days=8),
            registration_url="https://zoom.example.com/register",
        )
    )
    session.add(PortalMapping(id=mapping_id, school_id=school_id, webinar_id=webinar_id))
    session.add(
        EmailAutomation(
            id=automation_id,
            name="Follow-up",
            type="post_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="after",
            template_id=template_id,
        )
    )
    session.commit()

    seen: list[str | None] = []
    real = automation_runner.build_workshop_merge_replacements

    def spy(**kwargs):
        seen.append(kwargs.get("display_timezone"))
        return real(**kwargs)

    monkeypatch.setattr(automation_runner, "build_workshop_merge_replacements", spy)

    assert run_automations_check(session) == 1
    assert seen == ["America/Los_Angeles"]
    session.close()
