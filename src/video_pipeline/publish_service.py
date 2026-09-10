"""Turn a `chaptering` job into a published replay.

This half runs in the API process, not the ECS task: it is a few minutes of
Bedrock and Vimeo calls with no video file involved, so paying for a container
to do it would be waste. Everything it needs was left in S3 by the task.

Order matters at the end. Chapters go to Vimeo and are read back before the
webinar row is touched, because ``video_embed_code`` is what puts the player on
a school's page — writing it first would publish a replay whose chapter menu
might be empty or half-written. A failure anywhere leaves the job in
`chaptering` for the caller to record, and the whole sequence is safe to rerun:
the frames are still in S3 and the chapter list is replaced wholesale.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from src.config import settings
from src.integrations.vimeo_chapters import set_chapters
from src.video_pipeline import (
    artifact_store,
    chapter_confidence,
    job_service,
    notify,
    section_chapters,
    stage_progress,
    topic_segment,
)
from src.video_pipeline.chapter_build import NO_CAP, Chapter, apply_cap, build_chapters
from src.video_pipeline.embed_code import build_embed_code
from src.video_pipeline.ffmpeg_ops import Candidate
from src.video_pipeline.frame_classify import Classified, classify_frames
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.video_pipeline.video_title import video_title
from src.video_pipeline.transcript import Cue

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Chaptering could not complete. The message lands on the job row."""


def _video_ref(job: WebinarVideoJob) -> str:
    if not job.vimeo_video_id:
        raise PublishError("Job reached chaptering without a Vimeo video id")
    return f"{job.vimeo_video_id}:{job.vimeo_hash}" if job.vimeo_hash else job.vimeo_video_id


def _fetch_frames(
    prefix: str, work_dir: Path, spans: list[tuple[float, float]] | None = None
) -> list[Candidate]:
    """Download the classifiable frames named in ``candidates.json``.

    ``spans`` narrows the download to the windows around transcript boundaries,
    which is where the frames are actually read. Filtering before the download
    rather than before the vision call is deliberate: the sampler emits a frame
    per visual change across the whole recording, and most of them describe a
    presenter's head, not a section.

    The manifest's opening ``0.0`` entry has no file — it is an anchor for the
    chapter builder, not a frame — so it is skipped here.
    """
    manifest = artifact_store.load_json_artifact(prefix, artifact_store.CANDIDATES_FILENAME)
    if not isinstance(manifest, list):
        raise PublishError(f"{artifact_store.CANDIDATES_FILENAME} is not a list of frames")

    candidates: list[Candidate] = []
    for entry in manifest:
        filename = entry.get("file")
        if not filename:
            continue
        timestamp = float(entry["timestamp"])
        if spans is not None and not section_chapters.in_any_window(timestamp, spans):
            continue
        candidates.append(
            Candidate(
                index=int(entry["index"]),
                timestamp=timestamp,
                path=artifact_store.download_frame(prefix, filename, work_dir / filename),
            )
        )
    if not candidates and spans is None:
        raise PublishError(f"No frames to classify under {prefix}")
    return candidates


def _load_cues(prefix: str) -> list[Cue]:
    """Transcript cues, already on the trimmed video's clock.

    A missing transcript is not fatal: chapters still come from the frames, the
    Q&A boundary just falls back to the trailing speaker run and no title gets
    cross-checked.
    """
    try:
        raw = artifact_store.load_json_artifact(prefix, artifact_store.TRANSCRIPT_FILENAME)
    except artifact_store.ArtifactError as exc:
        logger.warning("No transcript artefact under %s (%s) — chaptering from frames alone", prefix, exc)
        return []
    if not isinstance(raw, list):
        return []
    return [
        Cue(start=float(c["start"]), end=float(c["end"]), text=str(c.get("text") or ""))
        for c in raw
    ]


def _classify(candidates: list[Candidate]) -> list[Classified]:
    frames = classify_frames(candidates)
    if not frames:
        raise PublishError("The vision model returned nothing for any frame")
    return frames


def _assemble(
    frames: list[Classified],
    cues: list[Cue],
    duration: float | None,
    sections: list[topic_segment.Section] | None = None,
) -> tuple[list[Chapter], bool]:
    """Build the chapter list, cross-check it, and apply the cap.

    With sections, the transcript decides the boundaries and the frames only
    title them. Without, the frames segment the recording as they always did,
    under a minimum-length floor — the crude stand-in for the judgement the
    transcript would have made.

    Built uncapped so the cap can report whether it actually dropped anything.
    """
    if sections:
        chapters = section_chapters.build_from_sections(
            sections, frames, cues=cues, max_chapters=NO_CAP
        )
    else:
        chapters, _runs = build_chapters(
            frames,
            cues=cues,
            duration=duration,
            max_chapters=NO_CAP,
            min_section_seconds=settings.video_min_section_seconds,
        )
    if not chapters:
        raise PublishError("No chapters could be derived from the classified frames")
    chapters = chapter_confidence.annotate(chapters, cues)
    return apply_cap(chapters, settings.video_max_chapters)


def _write_embed_code(job: WebinarVideoJob) -> None:
    """Put the player on the webinar's page — the step that makes it public.

    Skipped for an audit run, which exists to be watched in Vimeo before anyone
    decides the pipeline can be trusted with a school's page. Chapters are
    still set on the video above: they are what the audit is judging.
    """
    if job.audit_only:
        logger.info("Audit run %s — not writing an embed code to any webinar", job.id)
        return

    webinar = job.webinar
    if webinar is None:
        raise PublishError(f"Job {job.id} has no webinar to publish to")
    if not job.vimeo_player_embed_url:
        raise PublishError("Vimeo gave no player embed URL at upload — nothing to embed")
    # The same name the video carries on Vimeo. The iframe's title is what a
    # screen reader announces, so the two disagreeing would be a small lie told
    # only to the people who cannot see the player.
    webinar.video_embed_code = build_embed_code(job.vimeo_player_embed_url, video_title(job))


def publish(db: Session, job: WebinarVideoJob) -> WebinarVideoJob:
    """Chapter, publish, and mark ``job`` published.

    Raises:
        PublishError: for anything this stage can diagnose itself.
        VimeoError, ArtifactError, BedrockCallError: from the services below.
        The caller records whichever it gets on the job row.
    """
    if job.job_state is not JobState.CHAPTERING:
        raise PublishError(f"Job {job.id} is {job.state}, not chaptering")
    if not job.frames_prefix:
        raise PublishError("Job reached chaptering without a frames prefix")

    video_ref = _video_ref(job)
    with tempfile.TemporaryDirectory(prefix=f"chapter-{job.id}-") as tmp:
        stage_progress.record(db, job, stage_progress.LOADING_ARTIFACTS)
        cues = _load_cues(job.frames_prefix)

        # The transcript is read before the frames because it decides which
        # frames are worth reading at all.
        stage_progress.record(db, job, stage_progress.SEGMENTING_TRANSCRIPT)
        sections = topic_segment.detect_sections(cues)
        spans = section_chapters.windows(sections) if sections else None

        candidates = _fetch_frames(job.frames_prefix, Path(tmp), spans)
        if spans is not None and not candidates:
            # The windows landed where the sampler kept nothing. Reading every
            # frame is the slower answer, not a wrong one.
            logger.warning(
                "no sampled frame falls inside any section window for job %s — "
                "classifying the whole recording instead",
                job.id,
            )
            sections = []
            candidates = _fetch_frames(job.frames_prefix, Path(tmp))

        stage_progress.record(db, job, stage_progress.CLASSIFYING_FRAMES)
        frames = _classify(candidates)

    stage_progress.record(db, job, stage_progress.BUILDING_CHAPTERS)
    duration = float(job.source_duration_seconds) if job.source_duration_seconds else None
    chapters, truncated = _assemble(frames, cues, duration, sections)

    stage_progress.record(db, job, stage_progress.SETTING_CHAPTERS)
    confirmed = set_chapters(video_ref, [c.as_dict() for c in chapters])
    logger.info("Vimeo confirmed %d chapters on video %s", len(confirmed), video_ref)

    # Recorded only for a run that has an embed code to write. An audit run
    # skips the step entirely, and `expected_stages` leaves it out of the plan
    # so its timeline does not end on a step that will never happen.
    if not job.audit_only:
        stage_progress.record(db, job, stage_progress.WRITING_EMBED_CODE)
    _write_embed_code(job)

    published = job_service.advance(
        db,
        job,
        JobState.PUBLISHED,
        chapters=[c.as_dict() for c in chapters],
        chapters_truncated=truncated,
    )
    stage_progress.record(db, job, stage_progress.DONE)

    # After the row says published, not before: the alert says the replay is
    # live, and it should not be able to say so about a transaction that then
    # fails to commit. It cannot raise, so a mail problem never unpublishes
    # anything.
    notify.notify_published(db, job, video_title(job))
    return published
