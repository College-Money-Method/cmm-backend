"""Intake path: a Zoom recording becomes exactly one job, or none at all.

Both entry points (the `recording.completed` webhook and the reconcile sweep)
funnel through ``intake_recording``, so these cover both.
"""

from __future__ import annotations

import logging

import pytest

from src.video_pipeline import intake, job_service, task_dispatch
from src.video_pipeline.intake import intake_recording
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.video_pipeline.zoom_recording_fetch import NOT_READY_MESSAGE

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


def _failed_job(db, error: str) -> WebinarVideoJob:
    intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)
    job = db.query(WebinarVideoJob).one()
    job.state = JobState.FAILED.value
    job.error = error
    job.source_waits = 12
    db.commit()
    return job


def test_recording_event_rearms_a_job_that_gave_up_waiting_for_zoom(wired, db, monkeypatch):
    """A job created too early (reconcile during the live webinar) runs out of
    waits before Zoom is done. The later recording event means the files are
    ready now, so the job goes back in the queue instead of being ignored."""
    job = _failed_job(db, f"{NOT_READY_MESSAGE} {RECORDING_UUID} — HTTP 404 (code 3301)")
    dispatched = []
    monkeypatch.setattr(task_dispatch, "dispatch", lambda *a, **k: dispatched.append(a))

    assert intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID) is False

    db.expire_all()
    refreshed = db.get(WebinarVideoJob, job.id)
    assert refreshed.job_state is JobState.PENDING
    assert refreshed.source_waits == 0
    assert refreshed.error is None
    # Left for the sweeper: dispatching here too could start two ECS tasks.
    assert dispatched == []
    assert db.query(WebinarVideoJob).count() == 1


def test_recording_event_leaves_a_genuinely_failed_job_alone(wired, db):
    """A failure with a real cause needs a human, not a silent re-run."""
    job = _failed_job(db, "Chaptering failed: Bedrock refused")

    intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)

    db.expire_all()
    assert db.get(WebinarVideoJob, job.id).job_state is JobState.FAILED


def test_recording_event_does_not_touch_a_job_in_flight(wired, db):
    """The second recording event of a pair must not re-arm a job already queued."""
    intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)
    job = db.query(WebinarVideoJob).one()
    job_service.advance(db, job, JobState.PROCESSING)

    intake_recording(ZOOM_WEBINAR_ID, RECORDING_UUID)

    db.expire_all()
    refreshed = db.get(WebinarVideoJob, job.id)
    assert refreshed.job_state is JobState.PROCESSING
    assert refreshed.attempt == 0
