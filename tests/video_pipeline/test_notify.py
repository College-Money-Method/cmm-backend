"""Failure alerting for an unattended pipeline.

Nobody watches a run, so the alert is the only push signal that something broke.
It has to fire once per failure — not once per sweeper pass, and not never.
"""

from __future__ import annotations

import logging

import pytest

from src.config import settings
from src.video_pipeline import job_service, notify
from src.video_pipeline.states import JobState

RECORDING_UUID = "aB3/cD4+eF5=="
ALERT_ADDRESS = "video-alerts@collegemoneymethod.com"


@pytest.fixture
def sent(monkeypatch):
    """Capture alert sends instead of calling SES."""
    captured: list[dict] = []

    def fake_send_email(db, **kwargs):
        captured.append(kwargs)
        return None

    monkeypatch.setattr(notify, "send_email", fake_send_email)
    monkeypatch.setattr(settings, "video_pipeline_alert_email", ALERT_ADDRESS)
    return captured


@pytest.fixture
def job(db, webinar):
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )
    return row


def test_failure_alerts_exactly_once(db, job, sent):
    """The sweeper re-reads failed rows every few minutes; only the first alerts."""
    job_service.fail(db, job, "ffmpeg exited 1")

    assert len(sent) == 1
    assert sent[0]["to"] == ALERT_ADDRESS
    assert sent[0]["source"] == "video_pipeline"
    assert job.failed_notified_at is not None

    # Re-reads of the same failed job send nothing further.
    assert notify.notify_failure(db, job) is False
    assert notify.notify_failure(db, job) is False
    assert len(sent) == 1


def test_a_retry_that_fails_again_alerts_again(db, job, sent):
    """`retry` clears the stamp, so the second failure is a new failure."""
    job_service.fail(db, job, "ffmpeg exited 1")
    job_service.retry(db, job)

    assert job.failed_notified_at is None
    assert job.job_state is JobState.PENDING

    job_service.fail(db, job, "ffmpeg exited 1 again")

    assert len(sent) == 2


def test_alert_carries_the_error_and_the_webinar(db, job, webinar, sent):
    job_service.fail(db, job, "Bedrock returned no trim point")

    body = sent[0]["text"]
    assert "Bedrock returned no trim point" in body
    assert webinar.webinar_name in body
    assert sent[0]["webinar_id"] == webinar.id


def test_unset_alert_address_logs_rather_than_sending(db, job, sent, monkeypatch, caplog):
    """Empty is a supported config, but it must not fail silently."""
    monkeypatch.setattr(settings, "video_pipeline_alert_email", "")

    with caplog.at_level(logging.ERROR):
        job_service.fail(db, job, "ffmpeg exited 1")

    assert sent == []
    assert job.job_state is JobState.FAILED
    assert any("VIDEO_PIPELINE_ALERT_EMAIL is unset" in r.message for r in caplog.records)


def test_a_broken_send_never_loses_the_failure(db, job, sent, monkeypatch):
    """Recording *why* a job failed matters more than delivering the email."""
    def boom(db_, **kwargs):
        raise RuntimeError("SES unavailable")

    monkeypatch.setattr(notify, "send_email", boom)

    job_service.fail(db, job, "ffmpeg exited 1")
    db.refresh(job)

    assert job.job_state is JobState.FAILED
    assert job.error == "ffmpeg exited 1"


# ── the replay is live ───────────────────────────────────────────────────────


def test_publishing_tells_ops_where_to_watch_it(db, job, sent):
    job.vimeo_video_id = "1225339276"
    job.chapters = [{"timecode": 0, "title": "Introduction"}]
    db.commit()

    assert notify.notify_published(db, job, "Workshop #1 Paying for College") is True
    assert sent[0]["to"] == ALERT_ADDRESS
    assert sent[0]["subject"] == "[Video pipeline] Ready — Workshop #1 Paying for College"
    assert "https://vimeo.com/1225339276" in sent[0]["text"]


def test_an_audit_run_publishes_to_nobody_so_it_alerts_nobody(db, job, sent):
    """An audit run writes no embed code — there is no school-visible change to
    announce."""
    job.audit_only = True
    db.commit()

    assert notify.notify_published(db, job, "[Audit] Paying for College") is False
    assert sent == []


def test_an_unset_alert_address_is_not_an_error_here(db, job, sent, monkeypatch):
    monkeypatch.setattr(settings, "video_pipeline_alert_email", "")

    assert notify.notify_published(db, job, "Paying for College") is False
    assert sent == []


def test_a_broken_send_leaves_the_replay_published(db, job, sent, monkeypatch):
    """The embed code is already written by the time this runs. A mail server
    having a bad minute must not undo that."""
    def boom(db_, **kwargs):
        raise RuntimeError("SES unavailable")

    monkeypatch.setattr(notify, "send_email", boom)

    assert notify.notify_published(db, job, "Paying for College") is False
