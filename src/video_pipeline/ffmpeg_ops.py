"""ffmpeg operations: probe, stream-copy trim, and distinct-state frame sampling.

The sampling pass is the mechanical heart of the pipeline. It asks "was the change
big enough?" against ffmpeg's own scene score and emits the surviving frames plus
their timestamps in a single pass.

It used to ask "is this the same frame as the last one I kept?" via mpdecimate,
which needs no threshold and so looked like the safer choice. On a real webinar it
is not: mpdecimate keeps a frame as soon as any single 8x8 block differs enough,
and the presenter's own camera feed fills the frame, so an 82-minute recording
yielded 2,667 frames instead of the intended ~150 — 90% of them the same face in a
different pose. It also cannot be tuned out of that: sweeping its `hi` threshold
over a 3.3x range moved the count by 1.5% (2,667 -> 2,627). A scene score
separates the two cases cleanly, because a slide replacing the picture and a head
turning are not close on that scale. See ``build_sampling_filter``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# showinfo prints one stderr line per frame that survived the scene filter.
_PTS_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")

# Lead-in subtracted from a detected trim point. A stream copy snaps to the
# preceding keyframe and can land a few seconds early anyway; a beat of silence
# beats clipping the first word.
TRIM_LEAD_IN_SECONDS = 1.5


class FfmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


@dataclass(frozen=True)
class Candidate:
    """One distinct visual state: a frame file and when it appears."""

    index: int
    timestamp: float
    path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "file": self.path.name,
        }


def require_ffmpeg() -> None:
    """Fail early with a clear message when ffmpeg/ffprobe are missing."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise FfmpegError(
            f"{' and '.join(missing)} not found on PATH — install ffmpeg to continue"
        )


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    logger.debug("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise FfmpegError(
            f"{cmd[0]} exited {proc.returncode}:\n" + "\n".join(tail)
        )
    return proc


def probe_duration(path: Path) -> float:
    """Return a media file's duration in seconds."""
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise FfmpegError(f"could not read duration from {path}") from exc


def trim_stream_copy(source: Path, dest: Path, offset: float) -> Path:
    """Cut everything before `offset` without re-encoding.

    Near-instant, but the cut snaps to the preceding keyframe, so the real start
    can land up to a few seconds earlier than requested.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if offset <= 0:
        # Nothing to trim; copy the container so later stages have one input path.
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(source),
              "-c", "copy", "-movflags", "+faststart", str(dest)])
        return dest
    _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(source),
          "-c", "copy", "-movflags", "+faststart", str(dest)])
    return dest


def build_sampling_filter(
    *,
    fps: float,
    width: int,
    crop_w: float | None,
    crop_h: float | None,
    scene_threshold: float,
) -> str:
    """Compose the sampling filter chain.

    Order matters: the crop must precede the scene filter. Burned-in overlays that
    change constantly (a per-second Zoom clock, rolling captions) score on every
    frame, so with one left in the threshold has to be raised until it stops
    seeing slide changes too.

    The comma inside ``gt(scene, x)`` is escaped because the filtergraph parser
    splits on commas first — unescaped, ffmpeg reads the threshold as a filter.
    """
    chain = [f"fps={fps}", f"scale={width}:-1"]
    if crop_w and crop_h:
        chain.append(f"crop=iw*{crop_w}:ih*{crop_h}:0:0")
    chain += [rf"select=gt(scene\,{scene_threshold})", "showinfo"]
    return ",".join(chain)


def sample_distinct_frames(
    source: Path,
    frames_dir: Path,
    *,
    fps: float = 2.0,
    width: int = 960,
    crop_w: float | None = 0.83,
    crop_h: float | None = 0.88,
    scene_threshold: float = 0.05,
) -> list[Candidate]:
    """Extract one frame per distinct visual state, with its timestamp.

    `-fps_mode vfr` is load-bearing: without it ffmpeg re-pads the frames the
    filter rejected and the selection has no effect on the output at all.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.glob("frame_*.jpg"):
        stale.unlink()

    vf = build_sampling_filter(
        fps=fps,
        width=width,
        crop_w=crop_w,
        crop_h=crop_h,
        scene_threshold=scene_threshold,
    )
    proc = _run([
        "ffmpeg", "-y", "-v", "info", "-i", str(source),
        "-vf", vf, "-fps_mode", "vfr", "-q:v", "3",
        str(frames_dir / "frame_%04d.jpg"),
    ])

    timestamps = [float(m) for m in _PTS_RE.findall(proc.stderr or "")]
    files = sorted(frames_dir.glob("frame_*.jpg"))

    # The Nth showinfo line should describe the Nth output file. Verify rather than
    # assume — a mismatch means every chapter lands at the wrong time, which is
    # invisible in the frames themselves.
    if len(timestamps) != len(files):
        logger.warning(
            "showinfo reported %d frames but %d files were written; "
            "timestamps may be misaligned",
            len(timestamps),
            len(files),
        )
    count = min(len(timestamps), len(files))
    return [
        Candidate(index=i + 1, timestamp=timestamps[i], path=files[i])
        for i in range(count)
    ]


# silencedetect output, one pair of lines per detected silent run.
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")

# Only a silence that begins essentially at the head of the file counts as
# "dead opening"; a pause mid-talk is just a pause.
_LEADING_SILENCE_TOLERANCE = 2.0


def detect_speech_start(
    source: Path, *, noise_db: int = -30, min_silence: float = 2.0, max_offset: float = 900.0
) -> float:
    """Seconds of leading silence, for recordings that have no transcript.

    This is the degraded path. The normal trim point is semantic — the presenter
    talks through the dead opening, so silence cannot find it — and this only
    catches the case where the recording genuinely starts with dead air. It
    exists so a missing transcript still produces something sane rather than
    failing the job.

    Returns 0.0 whenever there is no leading silence, the detection fails, or the
    answer falls outside ``max_offset``. Erring towards no trim is deliberate:
    dead air looks sloppy, a late cut destroys content.
    """
    try:
        proc = _run([
            "ffmpeg", "-hide_banner", "-i", str(source),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f", "null", "-",
        ])
    except FfmpegError as exc:
        logger.warning("silencedetect failed, publishing untrimmed: %s", exc)
        return 0.0

    stderr = proc.stderr or ""
    starts = [float(m) for m in _SILENCE_START_RE.findall(stderr)]
    ends = [float(m) for m in _SILENCE_END_RE.findall(stderr)]
    if not starts or not ends or starts[0] > _LEADING_SILENCE_TOLERANCE:
        return 0.0

    speech_start = ends[0]
    if speech_start > max_offset:
        logger.warning(
            "silencedetect put first speech at %.1fs, beyond the %.0fs bound — not trimming",
            speech_start,
            max_offset,
        )
        return 0.0
    return max(0.0, speech_start - TRIM_LEAD_IN_SECONDS)
