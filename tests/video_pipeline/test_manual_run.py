"""Starting an audit run by hand.

The one thing every test here is defending: an audit run must be unable to
change anything. It is started against sessions that are live on real school
pages, so its job row carries no webinar to publish to, its source recording is
never deleted, and it refuses to start at all when there is no audit folder to
land in.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.video_pipeline import job_service, manual_run, task_dispatch
from src.video_pipeline.manual_source import SourceError
from src.video_pipeline.states import JobState
from src.video_pipeline.url_recording_fetch import UrlFetchError

RECORDING_UUID = "abc/def=="
S3_URL = "https://cmm-media.s3.amazonaws.com/raw/session.mp4"


@pytest.fixture
def audit_folder(monkeypatch):
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "/users/151255816/projects/30467578")


@pytest.fixture
def no_dispatch(monkeypatch):
    """ECS is not configured in tests; dispatch declines without raising."""
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)


@pytest.fixture
def zoom_has_the_recording(monkeypatch):
    monkeypatch.setattr(
        manual_run.zoom, "get_recording", lambda ref: {"uuid": RECORDING_UUID, "topic": "Session"}
    )


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setattr(manual_run.url_recording_fetch, "assert_public_url", lambda url: None)


# ── the destination check comes first ────────────────────────────────────────


def test_no_audit_folder_means_no_run(db, monkeypatch, no_dispatch):
    """Checked before Zoom is called: the alternative discovers it after the
    source is downloaded, trimmed and sampled."""
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "")
    monkeypatch.setattr(
        manual_run.zoom, "get_recording", lambda ref: pytest.fail("must not reach Zoom")
    )

    with pytest.raises(manual_run.RunError, match="VIMEO_AUDIT_FOLDER_URI"):
        manual_run.start(db, source="88812345678")


# ── zoom sources ─────────────────────────────────────────────────────────────


def test_a_meeting_id_is_keyed_on_the_recordings_own_uuid(
    db, audit_folder, no_dispatch, zoom_has_the_recording
):
    """Zoom answers a meeting ID with its latest instance. Keying on that
    instance's UUID is what makes a pasted meeting ID and the webhook for the
    same recording land on one row instead of two."""
    started = manual_run.start(db, source="881 2345 6789")

    assert started.job.zoom_recording_uuid == RECORDING_UUID
    assert started.job.source_url is None


def test_an_audit_run_has_no_webinar_and_is_flagged(
    db, audit_folder, no_dispatch, zoom_has_the_recording
):
    job = manual_run.start(db, source="88812345678").job

    assert job.webinar_id is None
    assert job.audit_only is True
    assert job.job_state is JobState.PENDING


def test_a_webinar_can_be_attached_to_name_the_video(
    db, webinar, audit_folder, no_dispatch, zoom_has_the_recording
):
    job = manual_run.start(db, source="88812345678", webinar_id=webinar.id).job

    assert job.webinar_id == webinar.id
    # Still audit-only: naming the video is not permission to publish to it.
    assert job.audit_only is True
    assert webinar.video_embed_code is None


def test_a_recording_zoom_cannot_find_is_reported_not_queued(db, audit_folder, monkeypatch):
    monkeypatch.setattr(manual_run.zoom, "get_recording", lambda ref: None)

    with pytest.raises(manual_run.RunError, match="no recording"):
        manual_run.start(db, source="88812345678")

    assert job_service.get_by_recording_uuid(db, RECORDING_UUID) is None


def test_a_recording_that_already_has_a_job_names_it(
    db, webinar, audit_folder, no_dispatch, zoom_has_the_recording
):
    """Processing it again would put a second task on one recording. The
    existing job is what the operator wants — its retry button."""
    existing, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=RECORDING_UUID
    )

    with pytest.raises(manual_run.RunConflict) as caught:
        manual_run.start(db, source="88812345678")

    assert caught.value.job.id == existing.id
    assert str(existing.id) in str(caught.value)


# ── url sources ──────────────────────────────────────────────────────────────


def test_a_url_source_records_the_url_and_a_synthetic_key(
    db, audit_folder, no_dispatch, public_url
):
    """A presigned URL has no stable identity to deduplicate on, and the UNIQUE
    key still has to be filled — so it gets a random one rather than the
    constraint being relaxed to let it through empty."""
    job = manual_run.start(db, source=S3_URL).job

    assert job.source_url == S3_URL
    assert job.zoom_recording_uuid.startswith("manual:")
    uuid.UUID(job.zoom_recording_uuid.removeprefix("manual:"))


def test_two_runs_of_the_same_url_are_two_jobs(db, audit_folder, no_dispatch, public_url):
    """Deliberate: the operator asked twice, and there is no webhook replay to
    defend against on this path."""
    first = manual_run.start(db, source=S3_URL).job
    second = manual_run.start(db, source=S3_URL).job

    assert first.id != second.id


def test_a_url_the_task_would_refuse_to_fetch_fails_at_the_paste(db, audit_folder, monkeypatch):
    """The same check the task runs, run here so the message reaches whoever
    typed the host rather than a job row ten minutes later."""
    monkeypatch.setattr(
        manual_run.url_recording_fetch,
        "_resolved_addresses",
        lambda host: ["10.0.3.14"],
    )

    with pytest.raises(UrlFetchError, match="not a public address"):
        manual_run.start(db, source="https://internal.example/session.mp4")


def test_a_source_that_parses_as_nothing_never_reaches_the_database(db, audit_folder):
    with pytest.raises(SourceError):
        manual_run.start(db, source="last Tuesday's one")


# ── dispatch ─────────────────────────────────────────────────────────────────


def test_the_job_survives_ecs_being_unavailable(
    db, audit_folder, no_dispatch, zoom_has_the_recording
):
    """`pending` with the sweeper still to come is a working state, so a failed
    dispatch is reported rather than raised."""
    started = manual_run.start(db, source="88812345678")

    assert started.dispatched is False
    assert started.job.job_state is JobState.PENDING


def test_a_dispatched_run_reports_it(db, audit_folder, monkeypatch, zoom_has_the_recording):
    monkeypatch.setattr(manual_run.task_dispatch, "dispatch", lambda db_, job: True)

    assert manual_run.start(db, source="88812345678").dispatched is True


# ── the operator's transcript ────────────────────────────────────────────────

VTT_URL = "https://cmm-media.s3.amazonaws.com/raw/session.vtt"


def test_a_transcript_url_is_recorded_against_the_job(
    db, audit_folder, no_dispatch, public_url
):
    """The whole point of the field: a pasted URL brings no captions, so
    without this the run trims from silence and chapters from frames alone."""
    job = manual_run.start(db, source=S3_URL, transcript_url=VTT_URL).job

    assert job.transcript_url == VTT_URL
    assert job.source_url == S3_URL


def test_a_transcript_can_be_supplied_for_a_zoom_source_too(
    db, audit_folder, no_dispatch, public_url, zoom_has_the_recording
):
    """Zoom hands over its own transcript, but only when the account had
    transcription switched on. Overriding it is the way to chapter a recording
    that did not."""
    job = manual_run.start(db, source="88812345678", transcript_url=VTT_URL).job

    assert job.transcript_url == VTT_URL


def test_no_transcript_url_stays_null(db, audit_folder, no_dispatch, public_url):
    job = manual_run.start(db, source=S3_URL).job

    assert job.transcript_url is None


def test_a_blank_transcript_url_is_not_supplied_rather_than_empty(
    db, audit_folder, no_dispatch, public_url
):
    """An operator who clears the field means the same as one who never filled
    it, so the column stays null instead of holding whitespace."""
    job = manual_run.start(db, source=S3_URL, transcript_url="   ").job

    assert job.transcript_url is None


def test_a_transcript_url_is_trimmed(db, audit_folder, no_dispatch, public_url):
    job = manual_run.start(db, source=S3_URL, transcript_url=f"  {VTT_URL}  ").job

    assert job.transcript_url == VTT_URL


def test_a_transcript_host_the_task_would_refuse_fails_at_the_paste(
    db, audit_folder, no_dispatch, monkeypatch
):
    """Same rule as the source URL, and for the same reason: the task fetches it
    from inside the VPC, and the message is only useful while the person who
    pasted it is still looking at the form."""

    def refuse(url):
        if url == VTT_URL:
            raise UrlFetchError("resolves to 127.0.0.1")

    monkeypatch.setattr(manual_run.url_recording_fetch, "assert_public_url", refuse)

    with pytest.raises(UrlFetchError, match="127.0.0.1"):
        manual_run.start(db, source=S3_URL, transcript_url=VTT_URL)

    assert job_service.get_by_recording_uuid(db, RECORDING_UUID) is None
