"""Workshop {{date}}/{{time}} render in the app-wide display zone, not stored UTC.

`Webinar.start_datetime` is stored UTC. For any US evening workshop that is
already the *next day* in UTC, so rendering the stored value straight out gets
the date wrong, not just the hour — which is what these tests pin down.

The zone is the recipient *school's*: its explicit `display_timezone` override,
else the one derived from its `state`, else the app-wide default. A counselor's
own `Contact.timezone` moves their Hub screen only and is deliberately absent
from this path — a person's screen setting must never decide what a family
reads.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.app_config.schemas import AppConfigUpdate
from src.auth.schemas import MePreferencesUpdate
from src.config import settings
from src.emails.workshop_merge_tags import build_workshop_merge_replacements
from src.schools import display_timezone as tz_module
from src.schools.display_timezone import (
    FALLBACK_TIMEZONE,
    STATE_TIMEZONES,
    resolve_display_timezone,
    timezone_for_state,
)
from src.schools.schemas import SchoolUpdate

# Bound before the autouse fixture below stubs the module attribute, so one test
# can still exercise the real database read.
from src.schools.display_timezone import app_default_timezone as real_app_default_timezone

# 7:00 PM Eastern on April 2 — stored as 23:00 UTC the same day under EDT.
EVENING_ET = datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc)
# 9:00 PM Pacific on April 2 — stored as 04:00 UTC on April *3*.
LATE_PT = datetime(2026, 4, 3, 4, 0, tzinfo=timezone.utc)


def _tags(
    start: datetime | None,
    *,
    school_state: str | None = None,
    school_timezone: str | None = None,
) -> dict[str, str]:
    return build_workshop_merge_replacements(
        school_name="Test High",
        family_label="Test High families",
        counselor_name="Casey Counselor",
        school_slug="test-high",
        school_state=school_state,
        school_timezone=school_timezone,
        workshop_name="FAFSA Basics",
        webinar_id=uuid.uuid4(),
        start_datetime=start,
        suggested_grades="11,12",
        cycle_name="2025-26",
    )


@pytest.fixture(autouse=True)
def _no_app_default(monkeypatch):
    """Assume no admin-set app-wide default unless a test says otherwise.

    Without this the resolver would try to read the config row from whatever
    database happens to be reachable, making these assertions depend on data.
    """
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: None)


# ── Zone resolution ─────────────────────────────────────────────────────────


def test_the_admin_set_default_wins_over_the_env_seed(monkeypatch):
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: "America/Denver")
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/Chicago")
    assert str(resolve_display_timezone()) == "America/Denver"


def test_the_env_seed_applies_until_an_admin_sets_a_default(monkeypatch):
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/Chicago")
    assert str(resolve_display_timezone()) == "America/Chicago"


def test_an_unreadable_config_row_reports_unset_instead_of_raising(monkeypatch):
    """A database that is down must not take an email send with it.

    Reporting "unset" lets the resolver fall through to the env seed, which is
    the behaviour every deployment had before the setting moved into the
    database.
    """
    import src.db.base as db_base

    def _boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(tz_module, "_app_default_cache", None)
    monkeypatch.setattr(db_base, "get_session_factory", _boom)
    assert real_app_default_timezone() is None


def test_an_unloadable_configured_default_degrades_instead_of_raising(monkeypatch):
    """A typo in the env must not take down every workshop send."""
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: "Also/Bogus")
    monkeypatch.setattr(settings, "workshop_display_timezone", "Not/AZone")
    assert str(resolve_display_timezone()) == FALLBACK_TIMEZONE


# ── Rendering ───────────────────────────────────────────────────────────────


def test_evening_workshop_keeps_its_own_date_and_local_hour(monkeypatch):
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: "America/New_York")
    tags = _tags(EVENING_ET)

    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "7:00 PM EDT"


def test_utc_rollover_does_not_advertise_the_workshop_a_day_late(monkeypatch):
    """The regression this whole feature exists for: 9 PM Pacific is stored as
    04:00 UTC the NEXT day, so the untranslated render named the wrong day."""
    # What the stored value says on its own, and must NOT be what goes out.
    assert LATE_PT.strftime("%A, %B") == "Friday, April" and LATE_PT.day == 3

    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: "America/Los_Angeles")
    tags = _tags(LATE_PT)
    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "9:00 PM PDT"


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("America/Los_Angeles", "9:00 PM PDT"),
        ("America/New_York", "12:00 AM EDT"),
        ("Pacific/Honolulu", "6:00 PM HST"),
    ],
)
def test_the_configured_zone_decides_how_one_instant_reads(monkeypatch, zone, expected):
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: zone)
    assert _tags(LATE_PT)["time"] == expected


def test_rendering_falls_back_to_the_env_seed_when_no_default_is_set(monkeypatch):
    monkeypatch.setattr(settings, "workshop_display_timezone", "America/Chicago")
    assert _tags(EVENING_ET)["time"] == "6:00 PM CDT"


def test_a_naive_datetime_is_left_where_it_is(monkeypatch):
    """No offset to convert from — shifting it would corrupt a correct value."""
    monkeypatch.setattr(tz_module, "app_default_timezone", lambda: "America/Los_Angeles")
    tags = _tags(datetime(2026, 4, 2, 19, 0))

    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "7:00 PM"


def test_missing_start_datetime_still_reads_tbd():
    tags = _tags(None)
    assert tags["date"] == "TBD"
    assert tags["time"] == "TBD"


# ── Write path ──────────────────────────────────────────────────────────────


def test_an_unsupported_app_default_is_rejected_before_it_can_be_stored():
    with pytest.raises(ValidationError):
        AppConfigUpdate(workshop_display_timezone="Mars/Olympus")


def test_a_blank_app_default_clears_the_setting():
    """Empty must become NULL (fall back to the env seed), never a string that
    fails to load as a zone at send time."""
    assert AppConfigUpdate(workshop_display_timezone="   ").workshop_display_timezone is None


# ── Per-counselor Hub preference ────────────────────────────────────────────


def test_a_counselors_own_zone_is_validated_against_the_same_list():
    with pytest.raises(ValidationError):
        MePreferencesUpdate(timezone="Mars/Olympus")
    assert MePreferencesUpdate(timezone="America/Denver").timezone == "America/Denver"


def test_clearing_a_counselors_zone_means_use_their_browser():
    assert MePreferencesUpdate(timezone="   ").timezone is None


def test_a_counselor_preference_cannot_reach_the_email_renderer():
    """The Hub preference is screen-only. The builder takes a zone now, but only
    a school-scoped one; a parameter carrying a person's or a viewer's zone
    would let one screen setting decide what every family reads, so the absence
    of that seam is the thing under test."""
    import inspect

    params = inspect.signature(build_workshop_merge_replacements).parameters
    zone_params = [p for p in params if "timezone" in p or p == "tz"]
    assert zone_params == ["school_timezone"]


# ── Per-school zone ─────────────────────────────────────────────────────────


def test_the_zone_comes_from_the_schools_state():
    """A California school advertises the same instant in Pacific, not in the
    app-wide Eastern default — the whole point of deriving from location."""
    assert _tags(EVENING_ET, school_state="CA")["time"] == "4:00 PM PDT"
    assert _tags(EVENING_ET, school_state="NY")["time"] == "7:00 PM EDT"


def test_a_late_workshop_keeps_the_right_date_in_the_schools_zone():
    """9:00 PM Pacific is already April 3 in UTC. A Pacific school must still
    read April 2, which is the failure that motivates all of this."""
    tags = _tags(LATE_PT, school_state="CA")
    assert tags["date"] == "Thursday, April 2, 2026"
    assert tags["time"] == "9:00 PM PDT"


def test_the_state_lookup_is_case_and_space_insensitive():
    assert timezone_for_state(" ca ") == "America/Los_Angeles"
    assert timezone_for_state("Ca") == "America/Los_Angeles"


def test_an_unknown_state_falls_through_instead_of_guessing():
    """A blank, a full state name or a territory outside the map must not
    resolve to a zone — being confidently an hour wrong is worse than using the
    app-wide default."""
    assert timezone_for_state(None) is None
    assert timezone_for_state("") is None
    assert timezone_for_state("California") is None
    assert timezone_for_state("PR") is None
    assert _tags(EVENING_ET, school_state="ZZ")["time"] == "7:00 PM EDT"


def test_an_override_beats_the_state_map():
    """Chattanooga is Eastern while Tennessee maps to Central. The override is
    the only thing standing between those schools and an hour's error."""
    assert _tags(EVENING_ET, school_state="TN")["time"] == "6:00 PM CDT"
    assert (
        _tags(EVENING_ET, school_state="TN", school_timezone="America/New_York")["time"]
        == "7:00 PM EDT"
    )


def test_every_mapped_state_uses_a_zone_the_picker_offers():
    """The map may only produce zones an admin could also have picked by hand,
    so a derived value and an override are always the same kind of thing."""
    assert set(STATE_TIMEZONES.values()) <= set(tz_module.TIMEZONE_NAMES)
    assert len(STATE_TIMEZONES) == 51  # 50 states + DC


def test_a_school_override_is_validated_against_the_supported_list():
    with pytest.raises(ValidationError):
        SchoolUpdate(display_timezone="Mars/Olympus")
    assert SchoolUpdate(display_timezone="America/Denver").display_timezone == "America/Denver"


def test_a_blank_school_override_clears_it_back_to_the_state_map():
    assert SchoolUpdate(display_timezone="   ").display_timezone is None


def test_no_school_at_all_is_the_app_wide_zone():
    """Admin screens and a preview with no school attached must keep getting a
    single reference zone, so two schools' webinars stay comparable."""
    assert str(resolve_display_timezone()) == FALLBACK_TIMEZONE
