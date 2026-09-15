"""The chaptering half: S3 artefacts in, a published webinar out.

The ordering rule is the point of most of these. ``video_embed_code`` is what
puts the player on a school's page, so it is written only after Vimeo has
confirmed the chapter list — a replay published with a broken or empty menu is
visible to families and nothing downstream would flag it.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.integrations.vimeo import VimeoError
from src.video_pipeline import (
    artifact_store,
    job_service,
    publish_service,
    stage_progress,
    topic_segment,
)
from src.video_pipeline.frame_classify import SPEAKER, TITLE_CARD, Classified
from src.video_pipeline.publish_service import PublishError, publish
from src.video_pipeline.states import JobState

PREFIX = "video-pipeline/frames/job/"
ALERT_ADDRESS = "video-alerts@collegemoneymethod.com"


@pytest.fixture(autouse=True)
def zoom_delete(monkeypatch):
    """Freeing the Zoom pool is the last step of a publish, so every test here
    reaches it. Stubbed for all of them — a unit test must never reach out to
    the real Zoom account — and returned so a test can assert what was deleted.
    """
    deleted: list[str] = []
    monkeypatch.setattr(
        publish_service.zoom, "delete_recording", lambda uuid_: deleted.append(uuid_) or True
    )
    return deleted


@pytest.fixture
def job(db, webinar):
    """A job parked in `chaptering`, exactly as the ECS task leaves it."""
    row, _ = job_service.create_from_recording(
        db, webinar_id=webinar.id, zoom_recording_uuid=f"rec-{uuid.uuid4()}"
    )
    job_service.advance(db, row, JobState.PROCESSING)
    row.vimeo_video_id = "987654321"
    row.vimeo_hash = "deadbeef01"
    row.vimeo_player_embed_url = "https://player.vimeo.com/video/987654321?h=deadbeef01"
    job_service.advance(
        db, row, JobState.CHAPTERING, frames_prefix=PREFIX, source_duration_seconds=3600
    )
    return row


def _frames():
    return [
        Classified(index=1, timestamp=0.0, file="frame_0001.jpg", type=SPEAKER, heading=""),
        Classified(
            index=2,
            timestamp=300.0,
            file="frame_0002.jpg",
            type=TITLE_CARD,
            heading="The Aid Formula",
        ),
    ]


@pytest.fixture(autouse=True)
def sections(monkeypatch):
    """The transcript pass, stubbed for every test in this file.

    It is one Bedrock call, so leaving it live would make these tests depend on
    a model's judgement and on network access. Empty is the frames-only route,
    which is what most of the assertions below describe; a test that wants the
    transcript spine appends the sections it wants found.
    """
    found: list[topic_segment.Section] = []
    monkeypatch.setattr(
        publish_service.topic_segment, "detect_sections", lambda cues: list(found)
    )
    return found


@pytest.fixture
def artefacts(monkeypatch, tmp_path):
    """Serve the S3 handoff from memory; frames land as real files on disk."""
    manifest = [
        {"index": 0, "timestamp": 0.0, "file": None},
        {"index": 1, "timestamp": 0.0, "file": "frame_0001.jpg"},
        {"index": 2, "timestamp": 300.0, "file": "frame_0002.jpg"},
    ]
    cues = [{"start": 298.0, "end": 303.0, "text": "on to the aid formula"}]

    def load(prefix, filename):
        if filename == artifact_store.CANDIDATES_FILENAME:
            return manifest
        return cues

    def download(prefix, filename, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(publish_service.artifact_store, "load_json_artifact", load)
    monkeypatch.setattr(publish_service.artifact_store, "download_frame", download)
    monkeypatch.setattr(publish_service, "classify_frames", lambda candidates: _frames())


@pytest.fixture
def vimeo_ok(monkeypatch):
    """Vimeo accepts and confirms whatever it is sent."""
    sent: list[list[dict]] = []

    def set_chapters(ref, chapters):
        sent.append(list(chapters))
        return [{"timecode": c["timecode"], "title": c["title"]} for c in chapters]

    monkeypatch.setattr(publish_service, "set_chapters", set_chapters)
    return sent


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    """Capture the ready alert instead of mailing it.

    Autouse because publishing sends one, and every test below that reaches the
    end of ``publish`` would otherwise use whatever address the environment
    supplies. The alert address is on the domain SES lets through even in
    sandbox mode, so an unpatched send here is a real email about a webinar that
    exists only in this file.
    """
    captured: list[dict] = []

    def fake_send_email(db, **kwargs):
        captured.append(kwargs)
        return None

    monkeypatch.setattr(publish_service.notify, "send_email", fake_send_email)
    monkeypatch.setattr(settings, "video_pipeline_alert_email", ALERT_ADDRESS)
    return captured


# ── guards ───────────────────────────────────────────────────────────────────


def test_a_job_in_another_state_is_refused(db, job, artefacts, vimeo_ok):
    job_service.advance(db, job, JobState.PUBLISHED)

    with pytest.raises(PublishError, match="not chaptering"):
        publish(db, job)


def test_a_job_without_frames_is_refused(db, job, artefacts, vimeo_ok):
    job.frames_prefix = None

    with pytest.raises(PublishError, match="frames prefix"):
        publish(db, job)


def test_a_job_without_a_vimeo_video_is_refused(db, job, artefacts, vimeo_ok):
    job.vimeo_video_id = None

    with pytest.raises(PublishError, match="Vimeo video id"):
        publish(db, job)


def test_a_prefix_holding_no_frames_is_refused(db, job, monkeypatch, vimeo_ok):
    """A manifest whose only entry is the opening anchor names no frame at all."""

    def load(prefix, filename):
        if filename == artifact_store.CANDIDATES_FILENAME:
            return [{"index": 0, "timestamp": 0.0, "file": None}]
        return []

    monkeypatch.setattr(publish_service.artifact_store, "load_json_artifact", load)

    with pytest.raises(PublishError, match="No frames"):
        publish(db, job)


# ── the happy path ───────────────────────────────────────────────────────────


def test_publishing_writes_the_embed_code_and_the_chapters(db, job, webinar, artefacts, vimeo_ok):
    publish(db, job)

    assert job.job_state is JobState.PUBLISHED
    assert webinar.video_embed_code.startswith(
        '<iframe src="https://player.vimeo.com/video/987654321?h=deadbeef01&amp;title=0'
    )
    assert [(c["timecode"], c["title"]) for c in job.chapters] == [
        (0, "Introduction"),
        (300, "The Aid Formula"),
    ]


def test_publishing_tells_ops_the_replay_is_live(db, job, artefacts, vimeo_ok, alerts):
    """The last step of a successful publish, and the only one that leaves the
    machine — so it is asserted here rather than left to the notify tests."""
    publish(db, job)

    assert len(alerts) == 1
    assert alerts[0]["to"] == ALERT_ADDRESS
    assert alerts[0]["webinar_id"] == job.webinar_id
    assert "Ready" in alerts[0]["subject"]


def test_a_failed_publish_tells_nobody_the_replay_is_live(db, job, artefacts, monkeypatch, alerts):
    """No embed code was written, so there is nothing to announce."""
    monkeypatch.setattr(
        publish_service, "set_chapters", lambda ref, chapters: (_ for _ in ()).throw(VimeoError("500"))
    )

    with pytest.raises(VimeoError):
        publish(db, job)

    assert alerts == []


def test_the_stored_chapters_carry_the_admin_only_fields(db, job, artefacts, vimeo_ok):
    """`source` and `confidence` exist for the admin screen, not for Vimeo."""
    publish(db, job)

    stored = {c["title"]: c for c in job.chapters}
    assert stored["The Aid Formula"]["source"] == "title_card"
    assert stored["The Aid Formula"]["confidence"] == "match"
    assert stored["Introduction"]["confidence"] == ""

    sent_keys = {key for chapter in vimeo_ok[0] for key in chapter}
    assert sent_keys == {"timecode", "title", "source", "confidence"}


def test_the_video_is_addressed_with_its_privacy_hash(db, job, artefacts, monkeypatch):
    refs: list[str] = []
    monkeypatch.setattr(
        publish_service,
        "set_chapters",
        lambda ref, chapters: refs.append(ref) or [dict(c) for c in chapters],
    )

    publish(db, job)

    assert refs == ["987654321:deadbeef01"]


def test_a_capped_chapter_list_is_recorded_as_truncated(db, job, monkeypatch, artefacts, vimeo_ok):
    monkeypatch.setattr(settings, "video_max_chapters", 1)

    publish(db, job)

    assert job.chapters_truncated is True
    assert len(job.chapters) == 1


def test_a_full_list_is_not_flagged_as_truncated(db, job, artefacts, vimeo_ok):
    publish(db, job)

    assert job.chapters_truncated is False


# ── the transcript spine ─────────────────────────────────────────────────────


def _downloads(monkeypatch) -> list[str]:
    """Record which frames were actually pulled out of S3."""
    asked: list[str] = []

    def download(prefix, filename, dest):
        asked.append(filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(publish_service.artifact_store, "download_frame", download)
    return asked


def test_only_the_frames_near_a_section_boundary_are_read(
    db, job, artefacts, sections, vimeo_ok, monkeypatch
):
    """The narrowing is the whole point: a frame far from any boundary describes
    a slide inside a section, and promoting it is what produced too many
    chapters."""
    asked = _downloads(monkeypatch)
    sections.append(topic_segment.Section(start=300.0, kind=topic_segment.CONTENT, label="Aid"))

    publish(db, job)

    assert asked == ["frame_0002.jpg"]


def test_the_transcript_decides_the_boundaries_and_the_deck_names_them(
    db, job, artefacts, sections, vimeo_ok
):
    sections += [
        topic_segment.Section(start=0.0, kind=topic_segment.INTRODUCTION, label="Welcome"),
        topic_segment.Section(
            start=300.0, kind=topic_segment.CONTENT, label="how aid is worked out"
        ),
    ]

    publish(db, job)

    # The second chapter carries the slide's wording, not the model's.
    assert [(c["timecode"], c["title"], c["source"]) for c in job.chapters] == [
        (0, "Introduction", "intro"),
        (300, "The Aid Formula", "title_card"),
    ]


def test_windows_that_catch_no_frame_fall_back_to_the_whole_recording(
    db, job, artefacts, sections, vimeo_ok, monkeypatch
):
    """Sections the sampler kept nothing near leave the chapters untitled. Reading
    every frame is the slower answer, not a wrong one."""
    asked = _downloads(monkeypatch)
    sections.append(topic_segment.Section(start=2000.0, kind=topic_segment.CONTENT, label="Aid"))

    publish(db, job)

    assert asked == ["frame_0001.jpg", "frame_0002.jpg"]
    assert [(c["timecode"], c["title"]) for c in job.chapters] == [
        (0, "Introduction"),
        (300, "The Aid Formula"),
    ]


# ── failure ordering ─────────────────────────────────────────────────────────


def test_a_vimeo_failure_leaves_the_webinar_unpublished(db, job, webinar, artefacts, monkeypatch):
    """The page keeps showing no replay rather than one with no chapter menu."""
    monkeypatch.setattr(
        publish_service,
        "set_chapters",
        lambda ref, chapters: (_ for _ in ()).throw(VimeoError("kept 1 of 2 chapters")),
    )

    with pytest.raises(VimeoError):
        publish(db, job)

    assert webinar.video_embed_code is None
    assert job.job_state is JobState.CHAPTERING


def test_no_usable_frames_fails_before_anything_is_published(db, job, monkeypatch, artefacts, webinar):
    """An all-blank classification would otherwise publish a chapterless replay."""
    monkeypatch.setattr(publish_service, "classify_frames", lambda candidates: [])
    monkeypatch.setattr(
        publish_service, "set_chapters", lambda *a: pytest.fail("must not reach Vimeo")
    )

    with pytest.raises(PublishError):
        publish(db, job)

    assert webinar.video_embed_code is None


def test_a_missing_transcript_is_not_fatal(db, job, monkeypatch, vimeo_ok, tmp_path):
    """Chapters come from the frames; the transcript only refines them."""

    def load(prefix, filename):
        if filename == artifact_store.TRANSCRIPT_FILENAME:
            raise artifact_store.ArtifactError("no such key")
        return [
            {"index": 0, "timestamp": 0.0, "file": None},
            {"index": 1, "timestamp": 0.0, "file": "frame_0001.jpg"},
            {"index": 2, "timestamp": 300.0, "file": "frame_0002.jpg"},
        ]

    monkeypatch.setattr(publish_service.artifact_store, "load_json_artifact", load)
    monkeypatch.setattr(
        publish_service.artifact_store,
        "download_frame",
        lambda prefix, filename, dest: (dest.parent.mkdir(parents=True, exist_ok=True), dest.write_bytes(b"j"), dest)[-1],
    )
    monkeypatch.setattr(publish_service, "classify_frames", lambda candidates: _frames())

    publish(db, job)

    assert job.job_state is JobState.PUBLISHED
    # Nothing was checked against speech, so no title is flagged either way.
    assert {c["confidence"] for c in job.chapters} == {""}


# ── audit runs ───────────────────────────────────────────────────────────────


@pytest.fixture
def audit_job(db):
    """An audit run in `chaptering`: no webinar, and nothing it may publish to."""
    row, _ = job_service.create_from_recording(
        db, zoom_recording_uuid=f"rec-{uuid.uuid4()}", audit_only=True
    )
    job_service.advance(db, row, JobState.PROCESSING)
    row.vimeo_video_id = "987654321"
    row.vimeo_hash = "deadbeef01"
    row.vimeo_player_embed_url = "https://player.vimeo.com/video/987654321?h=deadbeef01"
    job_service.advance(
        db, row, JobState.CHAPTERING, frames_prefix=PREFIX, source_duration_seconds=3600
    )
    return row


def test_an_audit_run_still_gets_its_chapters_set_on_vimeo(db, audit_job, artefacts, vimeo_ok):
    """The chapter menu is the main thing the audit is judging, so it has to be
    on the video before anyone opens the folder."""
    publish(db, audit_job)

    assert [(c["timecode"], c["title"]) for c in vimeo_ok[0]] == [
        (0, "Introduction"),
        (300, "The Aid Formula"),
    ]
    assert audit_job.job_state is JobState.PUBLISHED


def test_an_audit_run_writes_no_embed_code_anywhere(db, audit_job, webinar, artefacts, vimeo_ok):
    """`published` for an audit run means "in the folder, ready to watch" — no
    school page changed."""
    publish(db, audit_job)

    assert webinar.video_embed_code is None


def test_an_audit_run_attached_to_a_webinar_still_leaves_it_alone(
    db, audit_job, webinar, artefacts, vimeo_ok
):
    """A webinar is attached only to name the video. Treating that as consent to
    publish would make the safe option indistinguishable from the live one."""
    audit_job.webinar_id = webinar.id
    db.commit()

    publish(db, audit_job)

    assert webinar.video_embed_code is None
    assert audit_job.job_state is JobState.PUBLISHED


def test_a_production_run_with_no_webinar_is_refused(db, job, artefacts, vimeo_ok):
    """Only an audit run may have no webinar. A production job that somehow has
    none must not report itself published having published nothing."""
    job.webinar_id = None
    db.commit()

    with pytest.raises(PublishError, match="no webinar"):
        publish(db, job)


# ── the step timeline ────────────────────────────────────────────────────────


def test_publishing_records_each_step_in_order(db, job, artefacts, vimeo_ok):
    """The admin screen renders this list as a stepper, so the order it was
    written in is the order the work happened in."""
    publish(db, job)

    recorded = [e["stage"] for e in stage_progress.events_of(job)]
    chaptering = recorded[recorded.index(stage_progress.LOADING_ARTIFACTS) :]
    assert chaptering == [
        stage_progress.LOADING_ARTIFACTS,
        stage_progress.SEGMENTING_TRANSCRIPT,
        stage_progress.CLASSIFYING_FRAMES,
        stage_progress.BUILDING_CHAPTERS,
        stage_progress.SETTING_CHAPTERS,
        stage_progress.WRITING_EMBED_CODE,
        stage_progress.DELETING_ZOOM_COPY,
        stage_progress.DONE,
    ]


def test_an_audit_run_records_no_embed_code_step(db, audit_job, artefacts, vimeo_ok):
    """It writes none, and a step that never runs must not be shown as pending."""
    publish(db, audit_job)

    recorded = [e["stage"] for e in stage_progress.events_of(audit_job)]
    assert stage_progress.WRITING_EMBED_CODE not in recorded
    assert recorded[-1] == stage_progress.DONE


def test_the_zoom_copy_survives_a_run_that_never_reaches_published(
    db, job, artefacts, monkeypatch, zoom_delete
):
    """The reason the delete sits last. A job that breaks here goes back through
    the pipeline, and the Zoom cloud holds the only copy of the source that is
    not in Glacier — freeing it earlier turns a re-runnable failure into a lost
    recording.
    """
    monkeypatch.setattr(
        publish_service,
        "set_chapters",
        lambda ref, chapters: (_ for _ in ()).throw(VimeoError("kept 1 of 2 chapters")),
    )

    with pytest.raises(VimeoError):
        publish(db, job)

    assert zoom_delete == []


def test_a_published_run_frees_the_zoom_pool(db, job, artefacts, vimeo_ok, zoom_delete):
    publish(db, job)

    assert zoom_delete == [job.zoom_recording_uuid]
    assert job.job_state is JobState.PUBLISHED


def test_a_vimeo_failure_leaves_the_timeline_on_the_step_that_broke(db, job, artefacts, monkeypatch):
    monkeypatch.setattr(
        publish_service,
        "set_chapters",
        lambda ref, chapters: (_ for _ in ()).throw(VimeoError("kept 1 of 2 chapters")),
    )

    with pytest.raises(VimeoError):
        publish(db, job)

    assert stage_progress.current(job) == stage_progress.SETTING_CHAPTERS
