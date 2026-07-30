"""WebVTT parsing, chunking and re-serialisation for caption translation.

The translator only ever sees cue *text*. Timestamps, cue identifiers, settings
(``align:start position:10%``) and NOTE/STYLE/REGION blocks are held aside and
re-emitted verbatim, so a model that mangles or drops a line can never corrupt
timing.

SRT input is accepted too — the only structural difference that matters here is
``,`` vs ``.`` as the millisecond separator — because admins commonly have SRT
exports to hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "00:00:01.000 --> 00:00:04.000" with optional trailing cue settings.
# Hours are optional in WebVTT ("01:02.500"). SRT uses a comma separator.
_TIMING_RE = re.compile(
    r"^(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3})(?P<settings>.*)$"
)

# Blocks that carry no translatable dialogue and must survive untouched.
_METADATA_PREFIXES = ("NOTE", "STYLE", "REGION")


class VttError(Exception):
    """Raised when the uploaded file is not usable WebVTT/SRT."""


@dataclass
class Cue:
    """One caption cue. Only ``text`` is ever sent to the translator.

    ``start``/``end`` are seconds parsed from ``timing``, used to find the
    silences that make natural batch boundaries. ``timing`` remains the source
    of truth for output — it is re-emitted verbatim and never recomputed.
    """

    index: int
    timing: str
    text: str
    identifier: str | None = None
    start: float = 0.0
    end: float = 0.0


@dataclass
class VttDocument:
    """Parsed caption file: translatable cues plus everything else, in order.

    ``blocks`` is the rendering order. Each entry is either a raw string (header,
    NOTE/STYLE block) or an int — the index of a cue in ``cues``.
    """

    cues: list[Cue] = field(default_factory=list)
    blocks: list[str | int] = field(default_factory=list)


def parse(raw: str) -> VttDocument:
    """Parse WebVTT (or SRT) into cues + passthrough blocks.

    Raises:
        VttError: when the file contains no timed cues at all.
    """
    if not raw or not raw.strip():
        raise VttError("The caption file is empty.")

    # Normalise line endings and strip a UTF-8 BOM, which breaks the WEBVTT header.
    text = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    doc = VttDocument()
    # Blank-line-separated blocks is the structure both VTT and SRT share.
    for chunk in re.split(r"\n\s*\n", text):
        block = chunk.strip("\n")
        if not block.strip():
            continue
        _consume_block(block, doc)

    if not doc.cues:
        raise VttError(
            "No caption cues found. The file must be WebVTT or SRT with "
            "'00:00:00.000 --> 00:00:02.000' timing lines."
        )

    return doc


def _consume_block(block: str, doc: VttDocument) -> None:
    """Classify one block as a cue or as passthrough content."""
    lines = block.split("\n")

    if lines[0].strip().startswith(_METADATA_PREFIXES) or lines[0].strip().startswith("WEBVTT"):
        doc.blocks.append(block)
        return

    # A cue is [optional identifier line] + timing line + text lines.
    timing_at = 0 if _TIMING_RE.match(lines[0].strip()) else 1
    if timing_at >= len(lines) or not _TIMING_RE.match(lines[timing_at].strip()):
        # Not a cue and not recognised metadata — keep it rather than lose it.
        doc.blocks.append(block)
        return

    identifier = lines[0].strip() if timing_at == 1 else None
    body = "\n".join(lines[timing_at + 1 :]).strip()
    if not body:
        # Timed but empty — nothing to translate, emit as-is.
        doc.blocks.append(block)
        return

    timing = _normalise_timing(lines[timing_at].strip())
    start, end = _timing_bounds(timing)
    cue = Cue(
        index=len(doc.cues),
        timing=timing,
        text=body,
        identifier=identifier,
        start=start,
        end=end,
    )
    doc.cues.append(cue)
    doc.blocks.append(cue.index)


def _normalise_timing(timing: str) -> str:
    """Convert an SRT timing line to WebVTT form (comma → period milliseconds)."""
    match = _TIMING_RE.match(timing)
    if not match:
        return timing
    start = match.group("start").replace(",", ".")
    end = match.group("end").replace(",", ".")
    return f"{start} --> {end}{match.group('settings')}"


def _to_seconds(stamp: str) -> float:
    """Convert "HH:MM:SS.mmm" or "MM:SS.mmm" to seconds. 0.0 if unparseable."""
    try:
        parts = stamp.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0


def _timing_bounds(timing: str) -> tuple[float, float]:
    """Extract (start, end) seconds from a normalised timing line."""
    match = _TIMING_RE.match(timing)
    if not match:
        return 0.0, 0.0
    return _to_seconds(match.group("start")), _to_seconds(match.group("end"))


def serialize(doc: VttDocument, translations: dict[int, str]) -> str:
    """Rebuild a WebVTT document, substituting translated text per cue index.

    Cues missing from ``translations`` — or mapped to blank text — keep their
    source text, so a partially failed translation still yields a valid, playable
    file rather than one with silent gaps where captions should be.
    """
    parts: list[str] = []
    has_header = any(
        isinstance(b, str) and b.strip().startswith("WEBVTT") for b in doc.blocks
    )
    if not has_header:
        parts.append("WEBVTT")

    for block in doc.blocks:
        if isinstance(block, str):
            parts.append(block)
            continue
        cue = doc.cues[block]
        # Defensive: a blank or whitespace-only translation would emit a
        # timed-but-textless cue, i.e. a silent gap in the published track.
        translated = translations.get(cue.index)
        text = translated if translated and translated.strip() else cue.text
        lines = [cue.identifier, cue.timing, text] if cue.identifier else [cue.timing, text]
        parts.append("\n".join(lines))

    # WebVTT requires a blank line between blocks and a trailing newline.
    return "\n\n".join(parts) + "\n"


def chunk_cues(
    cues: list[Cue], char_budget: int, max_count: int, min_gap: float | None = None
) -> list[list[Cue]]:
    """Group cues into batches bounded by total characters and cue count.

    Keeps each Bedrock call comfortably inside Haiku's 8k output-token cap while
    staying large enough that the model sees neighbouring cues for context.
    A single oversized cue gets a batch to itself.

    With ``min_gap`` set, a silence of at least that many seconds also starts a
    new batch. Cues are mid-sentence fragments, so batching on speech pauses
    means a batch tends to hold whole sentences instead of an arbitrary window —
    the model translates coherent text and is less prone to reflowing content
    across the boundary.

    The size caps still apply: continuous narration can run minutes without a
    qualifying pause (observed: a 5-minute talk whose largest gap was 1.2s), so
    gaps alone would leave the whole transcript in one batch.
    """
    batches: list[list[Cue]] = []
    current: list[Cue] = []
    current_chars = 0

    for cue in cues:
        length = len(cue.text)
        silence = (
            min_gap is not None and current and (cue.start - current[-1].end) >= min_gap
        )
        over_budget = current and (
            current_chars + length > char_budget or len(current) >= max_count
        )
        if silence or over_budget:
            batches.append(current)
            current, current_chars = [], 0
        current.append(cue)
        current_chars += length

    if current:
        batches.append(current)
    return batches
