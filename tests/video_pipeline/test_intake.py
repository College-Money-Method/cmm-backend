"""Intake path: a Zoom recording becomes exactly one job, or none at all.

Both entry points (the `recording.completed` webhook and the reconcile sweep)
funnel through ``intake_recording``, so these cover both.
"""

from __future__ import annotations

import logging

import pytest

from src.video_pipeline import intake, task_dispatch
from src.video_pipeline.intake import intake_recording
from src.video_pipeline.models import WebinarVideoJob

from tests.video_pipeline.conftest import ZOOM_WEBINAR_ID

RECORDING_UUID = "aB3/cD4+eF5=="


@pytest.fixture
def wired(monkeypatch, sessionmaker_factory, webinar):
    """Point intake at the test database and stub the ECS launch away."""
    monkeypatch.setattr(intake, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)
    return sessionmaker_factory


def test_repeated_delivery_creates_exactly_one_job(wired, db):
    """Zoom retries a webhook it thinks failed; a replay must not double-process."""
    first = intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)
    second = intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)
    third = intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)

    assert [first, second, third] == [True, False, False]
    assert db.query(WebinarVideoJob).count() == 1


def test_unknown_webinar_is_logged_and_skipped(wired, db, caplog):
    """Zoom hosts calls this app knows nothing about. Not an error, just no replay."""
    with caplog.at_level(logging.WARNING):
        created = intake_recording("99999999999", RECORDING_UUID)

    assert created is False
    assert db.query(WebinarVideoJob).count() == 0
    assert any("unrecognised webinar" in record.message for record in caplog.records)


def test_missing_identifiers_never_raise(wired, db):
    """A malformed payload must not take down the background task."""
    assert intake_recording("", RECORDING_UUID) is False
    assert intake_recording(ZOOM_WEBINAR_ID, "") is False
    assert db.query(WebinarVideoJob).count() == 0
