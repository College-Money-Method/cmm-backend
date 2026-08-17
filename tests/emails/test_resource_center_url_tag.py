"""The ``{{resource_center_url}}`` tag is COMPUTED from the school portal slug,
never read off the legacy Airtable ``School.school_resource_center_url`` column.

Both tag builders (broadcast/communication and workshop) must agree, and both
must match what the counselor hub renders for the same school — see
``app/routes/hub/communications.tsx`` in cmm-frontend.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.emails.broadcast_send import build_merge_tag_replacements
from src.emails.school_links import resource_center_url
from src.emails.workshop_merge_tags import build_workshop_merge_replacements
from src.schools.models import School

ORIGIN = "https://app.collegemoneymethod.com"
WEBINAR_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


def _school(**overrides) -> School:
    defaults = dict(
        id=uuid.uuid4(),
        name="Test High",
        slug="test-high",
        # The legacy Airtable link — points at the old site and must be ignored.
        school_resource_center_url="https://legacy.example.com/old-portal",
        cmm_website_password="hunter2",
    )
    return School(**{**defaults, **overrides})


def _workshop_tags(school: School, origin: str | None = ORIGIN) -> dict[str, str]:
    return build_workshop_merge_replacements(
        school_name=school.name,
        family_label="Test High families",
        counselor_name="Casey Counselor",
        school_slug=school.slug,
        resource_center_password=school.cmm_website_password,
        workshop_name="FAFSA Basics",
        webinar_id=WEBINAR_ID,
        start_datetime=datetime(2026, 4, 3, 18, 0, tzinfo=timezone.utc),
        suggested_grades="11,12",
        cycle_name="2025-26",
        origin=origin,
    )


class TestHelper:
    def test_builds_portal_home_url(self):
        assert resource_center_url(ORIGIN, "test-high") == f"{ORIGIN}/school/test-high"

    def test_trailing_slash_on_origin_does_not_double_up(self):
        assert resource_center_url(f"{ORIGIN}/", "test-high") == f"{ORIGIN}/school/test-high"

    def test_no_slug_yields_empty_rather_than_a_wrong_link(self):
        assert resource_center_url(ORIGIN, None) == ""
        assert resource_center_url(ORIGIN, "") == ""


class TestBroadcastTags:
    def test_ignores_legacy_airtable_column(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.app_public_url", ORIGIN)
        tags = build_merge_tag_replacements(_school())
        assert tags["resource_center_url"] == f"{ORIGIN}/school/test-high"
        assert "legacy.example.com" not in tags["resource_center_url"]

    def test_slugless_school_renders_blank(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.app_public_url", ORIGIN)
        tags = build_merge_tag_replacements(_school(slug=None))
        assert tags["resource_center_url"] == ""

    def test_no_school_renders_blank(self, monkeypatch):
        monkeypatch.setattr("src.config.settings.app_public_url", ORIGIN)
        assert build_merge_tag_replacements(None)["resource_center_url"] == ""


class TestWorkshopTags:
    def test_ignores_legacy_airtable_column(self):
        tags = _workshop_tags(_school())
        assert tags["resource_center_url"] == f"{ORIGIN}/school/test-high"
        assert "legacy.example.com" not in tags["resource_center_url"]

    def test_slugless_school_renders_blank(self):
        assert _workshop_tags(_school(slug=None))["resource_center_url"] == ""

    def test_matches_the_broadcast_builder_for_the_same_school(self, monkeypatch):
        """A counselor previewing a communication and a family receiving an
        automation must land on the same page."""
        monkeypatch.setattr("src.config.settings.app_public_url", ORIGIN)
        school = _school()
        assert (
            _workshop_tags(school)["resource_center_url"]
            == build_merge_tag_replacements(school)["resource_center_url"]
        )
