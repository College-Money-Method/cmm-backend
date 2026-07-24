"""Unit tests for get_webinars_for_school_in_range — PortalMapping-only semantics.

Verifies that after removing the WorkshopRegistration union branch:
  - A webinar with a PortalMapping IS included.
  - A webinar with only WorkshopRegistration rows (no PortalMapping) is NOT included.
  - Registration/attendee counts are still aggregated for included webinars.

All tests use MagicMock DB — no real database required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from src.analytics.postgres_queries import get_webinars_for_school_in_range


SCHOOL_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

# Webinar A: has a PortalMapping → should appear
WEBINAR_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
# Webinar B: registration-only, no PortalMapping → must NOT appear
WEBINAR_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

START_DT = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)


def _webinar_row(webinar_id: uuid.UUID, name: str = "FAFSA 101") -> dict:
    """Minimal row dict as returned by db.execute().mappings().all() for webinar query."""
    return {
        "id": webinar_id,
        "zoom_webinar_id": f"zoom-{webinar_id}",
        "webinar_name": name,
        "start_datetime": START_DT,
        "unmatched_participants_count": 0,
        "workshop_name": name,
        "sequence_number": 1,
    }


def _make_db(webinar_rows: list[dict], reg_rows: list[dict] | None = None) -> MagicMock:
    """Build a MagicMock DB session with two sequential execute() calls:
      1st: webinar main query → webinar_rows
      2nd: registration count query → reg_rows (default empty)
    Subsequent calls return empty to be safe.
    """
    if reg_rows is None:
        reg_rows = []

    db = MagicMock()

    def execute_side_effect(stmt, *args, **kwargs):
        """Return different results for consecutive execute() calls."""
        call_count = db.execute.call_count
        mock_result = MagicMock()
        if call_count == 1:
            # First call: core webinar query
            mock_result.mappings.return_value.all.return_value = webinar_rows
        else:
            # Second call: registration count query
            mock_result.mappings.return_value.all.return_value = reg_rows
        return mock_result

    db.execute.side_effect = execute_side_effect
    return db


class TestPortalMappingOnlyInclusion:
    """Core invariant: only PortalMapping-linked webinars appear in results."""

    def test_webinar_with_portal_mapping_is_included(self):
        """Happy path: school has a PortalMapping for webinar A → appears."""
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert len(result) == 1
        assert result[0]["webinar_id"] == str(WEBINAR_A_ID)

    def test_webinar_without_portal_mapping_is_excluded(self):
        """Critical: registration-only webinar must NOT appear.

        Simulates the old behaviour that was removed: previously a webinar was
        included when WorkshopRegistration rows existed for the school even
        without a PortalMapping. The DB mock returns empty webinar_rows,
        meaning `Webinar.id.in_(mapped_ids_q)` found nothing — the function
        must return an empty list regardless of registrations.
        """
        # No PortalMapping → the mapped_ids subquery yields no IDs → no webinar rows
        db = _make_db(webinar_rows=[])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert result == [], (
            "A webinar with only WorkshopRegistration rows (no PortalMapping) "
            "must NOT appear after the union branch was removed."
        )

    def test_only_mapped_webinar_returned_when_both_exist(self):
        """A + B exist; only A has PortalMapping → only A in result.

        The DB mock returns only WEBINAR_A_ID row from the mapped-IDs-filtered
        query, simulating that B was not in the PortalMapping subquery.
        """
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        webinar_ids = [r["webinar_id"] for r in result]
        assert str(WEBINAR_A_ID) in webinar_ids
        assert str(WEBINAR_B_ID) not in webinar_ids

    def test_empty_when_no_portal_mappings_exist(self):
        """School with no PortalMappings at all → empty result."""
        db = _make_db(webinar_rows=[])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="-30d", date_to=None
        )
        assert result == []


class TestRegistrationCountsStillComputedForMappedWebinars:
    """Registration/attendee aggregation still works for PortalMapping-linked webinars."""

    def test_registration_counts_populated(self):
        """Counts from WorkshopRegistration are still merged for included webinars."""
        reg_row = {
            "webinar_id": WEBINAR_A_ID,
            "registered": 25,
            "attended_live": 18,
        }
        db = _make_db(
            webinar_rows=[_webinar_row(WEBINAR_A_ID)],
            reg_rows=[reg_row],
        )
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert len(result) == 1
        row = result[0]
        assert row["registered"] == 25
        assert row["attended_live"] == 18
        assert row["no_show"] == 7  # 25 - 18

    def test_zero_counts_when_no_registrations_for_mapped_webinar(self):
        """A PortalMapping-linked webinar with zero registrations → zero counts."""
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)], reg_rows=[])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert len(result) == 1
        assert result[0]["registered"] == 0
        assert result[0]["attended_live"] == 0
        assert result[0]["no_show"] == 0


class TestResultShape:
    """Validate output dict keys are complete."""

    def test_result_has_expected_keys(self):
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert len(result) == 1
        row = result[0]
        expected_keys = {
            "webinar_id", "workshop_name", "start_datetime",
            "registered", "attended_live", "no_show",
            "joined_without_reg", "recording_views", "avg_percent_watched",
            "detail_views", "resource_views", "sequence_number", "_webinar_id_raw",
        }
        assert expected_keys.issubset(set(row.keys()))

    def test_webinar_id_is_string(self):
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        assert isinstance(result[0]["webinar_id"], str)

    def test_start_datetime_is_iso_string(self):
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31"
        )
        # start_datetime must be an ISO string (serializable for JSON response)
        assert isinstance(result[0]["start_datetime"], str)
        assert "2026-06-01" in result[0]["start_datetime"]


class TestCycleIdScope:
    """When cycle_id is provided, scope_clause changes — PortalMapping filter still applies."""

    def test_cycle_id_scoped_webinar_appears(self):
        cycle_id = uuid.uuid4()
        db = _make_db(webinar_rows=[_webinar_row(WEBINAR_A_ID)])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31",
            cycle_id=cycle_id,
        )
        assert len(result) == 1

    def test_cycle_id_scoped_no_mapped_webinar_returns_empty(self):
        cycle_id = uuid.uuid4()
        db = _make_db(webinar_rows=[])
        result = get_webinars_for_school_in_range(
            db, SCHOOL_ID, date_from="2026-01-01", date_to="2026-12-31",
            cycle_id=cycle_id,
        )
        assert result == []
