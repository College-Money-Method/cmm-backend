"""Every link in outgoing email must be absolute.

Regression cover for a live bug: ``APP_PUBLIC_URL`` was unset in production, so
the workshop tag builder emitted a bare ``/school/<slug>/workshops/...`` path.
A mail client has no base URL to resolve that against — Gmail turned it into
``http:///school/annie-wright-schools/workshops/...`` and served a redirect
warning instead of the page. The rule these tests pin down: with no origin,
builders fall back to another absolute URL or return empty, never a path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.emails.school_links import email_origin, resource_center_url
from src.emails.workshop_merge_tags import build_workshop_merge_replacements

ORIGIN = "https://app.collegemoneymethod.com"
ZOOM_URL = "https://zoom.us/webinar/register/abc123"
WEBINAR_ID = uuid.UUID("dcdcb7e5-0000-0000-0000-000000000000")

# Tags whose values are URLs, and so must never be a bare path.
_LINK_TAGS = ("resource_center_url", "registration_link", "recording_link", "workshop_detail_url")


@pytest.fixture
def origin_settings(monkeypatch):
    """Set both origin settings; returns a setter for individual overrides."""

    def _set(app_public_url: str, frontend_url: str = ""):
        monkeypatch.setattr("src.config.settings.app_public_url", app_public_url)
        monkeypatch.setattr("src.config.settings.frontend_url", frontend_url)

    return _set


def _workshop_tags(origin: str | None, *, school_slug: str | None = "annie-wright-schools"):
    return build_workshop_merge_replacements(
        school_name="Annie Wright Schools",
        family_label="Annie Wright families",
        counselor_name="Casey Counselor",
        school_slug=school_slug,
        workshop_name="Understanding How To Qualify For Financial Aid",
        webinar_id=WEBINAR_ID,
        start_datetime=datetime(2026, 4, 3, 18, 0, tzinfo=timezone.utc),
        suggested_grades="11,12",
        cycle_name="2025-26",
        registration_url=ZOOM_URL,
        resources=[{"id": "res-1", "name": "FAFSA Guide", "link": "https://cdn.example.com/f.pdf"}],
        origin=origin,
    )


class TestEmailOrigin:
    def test_app_public_url_wins(self, origin_settings):
        origin_settings(ORIGIN, "http://localhost:5173")
        assert email_origin() == ORIGIN

    def test_falls_back_to_frontend_url(self, origin_settings):
        origin_settings("", ORIGIN)
        assert email_origin() == ORIGIN

    def test_trailing_slash_and_whitespace_stripped(self, origin_settings):
        origin_settings(f"  {ORIGIN}/  ")
        assert email_origin() == ORIGIN

    def test_both_blank_yields_empty_not_a_path(self, origin_settings):
        origin_settings("", "")
        assert email_origin() == ""


class TestResourceCenterUrl:
    def test_no_origin_yields_empty_rather_than_a_relative_path(self):
        """The exact shape of the production bug: "/school/<slug>"."""
        assert resource_center_url("", "annie-wright-schools") == ""
        assert resource_center_url(None, "annie-wright-schools") == ""


class TestWorkshopTagsWithoutOrigin:
    @pytest.mark.parametrize("origin", ["", None])
    def test_no_link_tag_is_a_bare_path(self, origin):
        tags = _workshop_tags(origin)
        for tag in _LINK_TAGS:
            assert not tags[tag].startswith("/"), f"{tag} is a relative path: {tags[tag]!r}"

    @pytest.mark.parametrize("origin", ["", None])
    def test_workshop_page_tags_fall_back_to_the_registration_url(self, origin):
        """A Zoom link is the wrong page but a working one; a hostless link is
        neither, so the absolute fallback wins."""
        tags = _workshop_tags(origin)
        assert tags["registration_link"] == ZOOM_URL
        assert tags["recording_link"] == ZOOM_URL
        assert tags["workshop_detail_url"] == ZOOM_URL

    @pytest.mark.parametrize("origin", ["", None])
    def test_resources_list_falls_back_to_each_resources_own_link(self, origin):
        assert "https://cdn.example.com/f.pdf" in _workshop_tags(origin)["resources_list"]
        assert "(/school/" not in _workshop_tags(origin)["resources_list"]

    def test_with_an_origin_the_tags_point_at_the_portal_page(self):
        tags = _workshop_tags(ORIGIN)
        expected = (
            f"{ORIGIN}/school/annie-wright-schools/workshops/"
            "understanding-how-to-qualify-for-financial-aid-dcdcb7e5?via=email"
        )
        assert tags["recording_link"] == expected
        assert tags["workshop_detail_url"] == expected
        assert tags["resource_center_url"] == f"{ORIGIN}/school/annie-wright-schools"
        assert f"{ORIGIN}/school/annie-wright-schools/resources/res-1?via=email" in tags["resources_list"]

    def test_trailing_slash_on_origin_does_not_double_up(self):
        assert "//school/" not in _workshop_tags(f"{ORIGIN}/")["recording_link"]

    def test_slugless_school_never_emits_a_slugless_path(self):
        tags = _workshop_tags(ORIGIN, school_slug=None)
        assert tags["resource_center_url"] == ""
        assert tags["recording_link"] == ZOOM_URL
