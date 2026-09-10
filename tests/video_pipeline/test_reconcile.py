"""The hourly catch-up sweep for recordings whose webhook never arrived.

A missed `recording.completed` leaves a recording in Zoom's cloud pool with
nothing to process or delete it. A full pool blocks Zoom from recording future
webinars, which no later retry can undo.
"""

from __future__ import annotations

import pytest

from src.video_pipeline import intake, reconcile, task_dispatch
from src.video_pipeline.models import WebinarVideoJob

from tests.video_pipeline.conftest import ZOOM_WEBINAR_ID


@pytest.fixture
def wired(monkeypatch, sessionmaker_factory, webinar):
    monkeypatch.setattr(intake, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr(reconcile, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)
    return sessionmaker_factory


def _listing(monkeypatch, recordings):
    monkeypatch.setattr(reconcile, "list_account_recordings", lambda *a, **k: recordings)


def _recording(uuid_: str) -> dict:
    return {"uuid": uuid_, "id": ZOOM_WEBINAR_ID}


def test_orphaned_recordings_get_jobs(wired, db, monkeypatch):
    _listing(monkeypatch, [_recording("rec-one"), _recording("rec-two")])

    assert reconcile.reconcile_recordings() == 2
    assert db.query(WebinarVideoJob).count() == 2


def test_recordings_that_already_have_a_job_are_skipped(wired, db, monkeypatch):
    """The sweep deliberately re-offers everything in the window every hour."""
    _listing(monkeypatch, [_recording("rec-one"), _recording("rec-two")])
    reconcile.reconcile_recordings()

    _listing(monkeypatch, [_recording("rec-one"), _recording("rec-two"), _recording("rec-three")])

    assert reconcile.reconcile_recordings() == 1
    assert db.query(WebinarVideoJob).count() == 3


def test_a_failed_listing_is_not_read_as_an_empty_one(wired, db, monkeypatch):
    """None means Zoom did not answer. Concluding "no orphans" from that is wrong."""
    _listing(monkeypatch, None)

    assert reconcile.reconcile_recordings() == 0
    assert db.query(WebinarVideoJob).count() == 0


def test_an_empty_window_is_not_an_error(wired, db, monkeypatch):
    _listing(monkeypatch, [])

    assert reconcile.reconcile_recordings() == 0


def test_reconcile_never_raises(wired, db, monkeypatch):
    """It runs on a scheduler thread with nothing above it to catch anything."""
    def boom(*args, **kwargs):
        raise RuntimeError("Zoom API exploded")

    monkeypatch.setattr(reconcile, "list_account_recordings", boom)

    assert reconcile.reconcile_recordings() == 0
