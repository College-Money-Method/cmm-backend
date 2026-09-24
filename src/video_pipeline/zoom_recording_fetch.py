"""Resolve and download the files of one Zoom cloud recording.

A recording is a *set* of files — several MP4 renditions, an M4A, chat, and
(when the account has it enabled) a VTT transcript. The pipeline wants exactly
two of them: the MP4 that shows the shared screen alongside the speaker, and
the TRANSCRIPT.

Download URLs are re-fetched here on every run rather than read from something
saved at webhook time. Zoom's ``download_url`` is only usable with a credential,
so re-deriving it from S2S OAuth is what keeps the pipeline free of Zoom
credentials at rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from src.integrations import zoom

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, read=300.0)
_STREAM_CHUNK = 8 * 1024 * 1024


# Zoom's code for "the recording exists, the cloud has not finished making it
# available yet". It arrives as a 404, the same status as a recording that was
# deleted or never existed, so the code is the only thing separating "come back
# later" from "give up".
_STILL_PROCESSING = 3301

# Opening words of every ``RecordingNotReadyError``. A job that runs out of
# waits is failed with this message, and intake reads it back to tell "Zoom was
# slow" apart from a real failure when ``recording.completed`` finally lands.
NOT_READY_MESSAGE = "Zoom is still processing the recording"


class RecordingFetchError(RuntimeError):
    """The recording could not be resolved or downloaded."""


class RecordingNotReadyError(RecordingFetchError):
    """Zoom has the recording but is still processing it.

    Its own subclass because the caller's response is different in kind: this
    is not a run that failed, it is a run that started too early. Zoom fires
    ``recording.completed`` when it finishes *recording*, which can be minutes
    ahead of the files being fetchable, so a job dispatched straight off the
    webhook routinely arrives before the source does.
    """


@dataclass(frozen=True)
class FetchedRecording:
    """Local paths for one downloaded recording."""

    video_path: Path
    transcript_path: Path | None
    duration_seconds: int
    topic: str
    # Wall-clock instant Zoom began recording, as Zoom reports it. Distinct from
    # both the webinar's scheduled start and the "actual start time" the UI
    # report shows (the host opens the room first, often minutes early), and the
    # only one of the three the transcript's clock is measured from. None when
    # Zoom omits it.
    recording_start: datetime | None
    # Zoom's camera-only rendition, when it made one and it downloaded. Only a
    # trailer reel reads it (``reel_sources``), so it is never worth failing over.
    camera_path: Path | None = None


def parse_start(raw: object) -> datetime | None:
    """Zoom's ISO-8601 ``start_time``, or None if it is missing or malformed.

    Never raises: the recording is still perfectly publishable without it. The
    Q&A causality check reads it, and so does the reconcile sweep, to tell a
    recording that is still live from one whose webhook was missed.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Zoom recording start_time is unparseable: %r", raw)
        return None


def _files(payload: dict) -> list[dict]:
    return [f for f in (payload.get("recording_files") or []) if isinstance(f, dict)]


# Zoom's ``recording_type`` values, best rendition first. The replay has to show
# the slides, so anything carrying the shared screen outranks any camera-only
# view. Size cannot stand in for this: a webinar of static slides composites to a
# *smaller* file than the speaker camera beside it, so "largest MP4" quietly
# published the speaker-only rendition on a real recording.
_VIEW_PREFERENCE = (
    "shared_screen_with_speaker_view",
    "shared_screen_with_speaker_view(cc)",
    "shared_screen_with_gallery_view",
    "shared_screen",
    "speaker_view",
    "active_speaker",
    "gallery_view",
)


def _view_rank(recording_type: str) -> int:
    """Where a rendition sits in ``_VIEW_PREFERENCE``; unknown types sort last."""
    try:
        return _VIEW_PREFERENCE.index(recording_type)
    except ValueError:
        return len(_VIEW_PREFERENCE)


def select_video_file(payload: dict) -> dict:
    """Pick the MP4 to publish: the best available view, largest within that view.

    Zoom returns several MP4 entries for the same instance (shared screen with
    speaker view, speaker view alone, gallery view). ``recording_type`` names the
    view directly, so it decides; file size only breaks a tie between entries of
    the same type, and carries renditions Zoom labels with something we have not
    seen before.
    """
    videos = [
        f
        for f in _files(payload)
        if (f.get("file_type") or "").upper() == "MP4"
        and (f.get("status") or "completed").lower() == "completed"
    ]
    if not videos:
        raise RecordingFetchError("Zoom recording has no completed MP4 file")
    chosen = min(
        videos,
        key=lambda f: (
            _view_rank((f.get("recording_type") or "").strip().lower()),
            -int(f.get("file_size") or 0),
        ),
    )
    logger.info(
        "Selected Zoom rendition %s — type=%s size=%s",
        chosen.get("id"),
        chosen.get("recording_type") or "unlabelled",
        chosen.get("file_size"),
    )
    return chosen


def select_transcript_file(payload: dict) -> dict | None:
    """Pick the VTT transcript, or None when the account did not produce one.

    Missing is a real state, not an error: audio transcript is an account
    setting and can be off. The caller falls back to silence detection and logs
    loudly, because a missing transcript degrades trim quality on every future
    recording until someone flips the setting.
    """
    for f in _files(payload):
        if (f.get("file_type") or "").upper() == "TRANSCRIPT":
            return f
    return None


# Camera-only renditions, best first: the presenter's face with no slide, which
# a trailer reel cuts to between shots of the shared screen.
CAMERA_RECORDING_TYPES = ("active_speaker", "speaker_view")


def select_camera_file(payload: dict) -> dict | None:
    """The completed camera-only MP4, or None when Zoom made none."""
    by_type = {
        (f.get("recording_type") or "").strip().lower(): f
        for f in _files(payload)
        if (f.get("file_type") or "").upper() == "MP4"
        and (f.get("status") or "completed").lower() == "completed"
        and f.get("download_url")
    }
    return next((by_type[t] for t in CAMERA_RECORDING_TYPES if t in by_type), None)


def download_camera(payload: dict, token: str, dest: Path) -> Path | None:
    """Download the camera-only rendition to `dest`; None when there is none or it fails."""
    camera = select_camera_file(payload)
    if camera is None:
        logger.info("Zoom made no camera-only rendition of this recording")
        return None
    try:
        return _download(camera["download_url"], token, dest)
    except RecordingFetchError as exc:
        logger.warning("Camera rendition download failed — continuing without it: %s", exc)
        return None


def _download(url: str, token: str, dest: Path) -> Path:
    """Stream one recording file to disk.

    Streamed rather than buffered: these are 1-2 GB files and the task has 4 GB
    of memory it also needs for ffmpeg.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in resp.iter_bytes(_STREAM_CHUNK):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise RecordingFetchError(f"Downloading {dest.name} from Zoom failed: {exc}") from exc

    if dest.stat().st_size == 0:
        raise RecordingFetchError(f"Zoom returned an empty file for {dest.name}")
    logger.info("Downloaded %s — %d bytes", dest.name, dest.stat().st_size)
    return dest


def fetch_recording(recording_uuid: str, work_dir: Path) -> FetchedRecording:
    """Download the MP4 and (when present) the VTT for ``recording_uuid``."""
    try:
        payload = zoom.get_recording(recording_uuid)
    except zoom.ZoomApiError as exc:
        # Zoom said why. Repeating it verbatim is the whole value: "no recording"
        # sent an operator looking for a deleted file when the real answer was a
        # scope the Server-to-Server app had never been granted.
        if exc.code == _STILL_PROCESSING:
            raise RecordingNotReadyError(
                f"{NOT_READY_MESSAGE} {recording_uuid} — {exc}"
            ) from exc
        raise RecordingFetchError(f"Zoom refused the recording {recording_uuid} — {exc}") from exc
    if payload is None:
        raise RecordingFetchError(
            f"Cannot fetch recording {recording_uuid} — Zoom credentials are not configured"
        )

    video = select_video_file(payload)
    download_url = video.get("download_url")
    if not download_url:
        raise RecordingFetchError("Zoom recording MP4 has no download_url")

    token = zoom.recording_access_token()
    video_path = _download(download_url, token, work_dir / "source.mp4")

    transcript_path: Path | None = None
    transcript = select_transcript_file(payload)
    if transcript and transcript.get("download_url"):
        try:
            transcript_path = _download(
                transcript["download_url"], token, work_dir / "source.vtt"
            )
        except RecordingFetchError as exc:
            # A missing transcript is survivable (silence-detection fallback);
            # failing the whole job over it is not worth it.
            logger.warning("Transcript download failed — continuing without it: %s", exc)
    else:
        logger.warning(
            "Zoom recording %s has no TRANSCRIPT file — audio transcript is "
            "probably disabled on the account; falling back to silence detection",
            recording_uuid,
        )

    return FetchedRecording(
        video_path=video_path,
        transcript_path=transcript_path,
        camera_path=download_camera(payload, token, work_dir / "camera.mp4"),
        duration_seconds=int(payload.get("duration") or 0) * 60,
        topic=str(payload.get("topic") or ""),
        recording_start=parse_start(payload.get("start_time")),
    )
