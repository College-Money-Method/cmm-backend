"""Transcript access for the video pipeline.

Parsing is delegated to `src/content/vtt_parser.py`, which already handles SRT,
cue settings and metadata blocks and is covered by tests. This module adds the
two things the pipeline needs on top: a minimal cue type, and re-basing onto the
trimmed video's clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.content.vtt_parser import VttError, parse


@dataclass(frozen=True)
class Cue:
    """One transcript cue, reduced to what the pipeline reads.

    Deliberately not `vtt_parser.Cue`: that type keeps a verbatim `timing`
    string as its source of truth for re-serialisation, which would silently
    disagree with `start`/`end` once the cues are re-based onto a trimmed clock.
    """

    start: float
    end: float
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "text": self.text}


def load_cues(path: Path) -> list[Cue]:
    """Parse a Zoom transcript into cues. Raises VttError on unusable input."""
    document = parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    return [
        Cue(start=cue.start, end=cue.end, text=cue.text.strip())
        for cue in document.cues
        if cue.text.strip()
    ]


def rebase(cues: list[Cue], offset: float) -> list[Cue]:
    """Shift cues onto the trimmed video's clock, dropping anything before the cut.

    Frame timestamps come from the trimmed file, so raw cue times are off by the
    trim offset. Normalising here keeps every later stage on one clock.
    """
    out: list[Cue] = []
    for cue in cues:
        if cue.end <= offset:
            continue
        out.append(
            Cue(
                start=max(0.0, cue.start - offset),
                end=max(0.0, cue.end - offset),
                text=cue.text,
            )
        )
    return out


def format_timestamp(seconds: float) -> str:
    """Format seconds as H:MM:SS for logs, reports, and prompts."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


__all__ = ["Cue", "VttError", "format_timestamp", "load_cues", "rebase"]
