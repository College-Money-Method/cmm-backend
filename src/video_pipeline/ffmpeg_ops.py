"""ffmpeg operations: probe, stream-copy trim, and distinct-state frame sampling.

The sampling pass is the mechanical heart of the pipeline. It answers "is this the
same frame as the last one I kept?" via mpdecimate rather than "was the change big
enough?" via a tuned scene threshold, and emits the surviving frames plus their
timestamps in a single pass.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# showinfo prints one stderr line per frame that survived mpdecimate.
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
    *, fps: float, width: int, crop_w: float | None, crop_h: float | None
) -> str:
    """Compose the sampling filter chain.

    Order matters: crop must precede mpdecimate. Burned-in overlays that change
    constantly (moving speaker PiP, per-second Zoom clock, rolling captions) make
    every frame differ from the last, so without the crop mpdecimate drops nothing
    and a 90-minute file yields thousands of "distinct" states instead of ~150.
    """
    chain = [f"fps={fps}", f"scale={width}:-1"]
    if crop_w and crop_h:
        chain.append(f"crop=iw*{crop_w}:ih*{crop_h}:0:0")
    chain += ["mpdecimate", "showinfo"]
    return ",".join(chain)


def sample_distinct_frames(
    source: Path,
    frames_dir: Path,
    *,
    fps: float = 2.0,
    width: int = 960,
    crop_w: float | None = 0.83,
    crop_h: float | None = 0.88,
) -> list[Candidate]:
    """Extract one frame per distinct visual state, with its timestamp.

    `-fps_mode vfr` is load-bearing: without it ffmpeg re-pads the dropped frames
    and mpdecimate has no effect on the output at all.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.glob("frame_*.jpg"):
        stale.unlink()

    vf = build_sampling_filter(fps=fps, width=width, crop_w=crop_w, crop_h=crop_h)
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
