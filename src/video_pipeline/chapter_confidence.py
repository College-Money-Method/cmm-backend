"""Cross-check each extracted chapter title against what was said near it.

Presenters narrate their transitions: they read the section title aloud around
the time they switch to it. That makes the transcript a cheap *confirmation* of
a title the vision model read off a card, though a poor anchor for finding one —
spoken titles are paraphrased, ASR-damaged and loosely timed, which is why the
frame stays the source of truth.

The result is advisory and never removes a chapter. A presenter who simply does
not read a title aloud would otherwise lose a real chapter, and that is a worse
failure than an admin glancing at a flag. What the flag is for is the aggregate:
a high no-match rate across the first runs is the signal that the classification
prompt is calling content slides title cards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from src.config import settings
from src.video_pipeline.chapter_build import Chapter
from src.video_pipeline.frame_classify import TITLE_CARD
from src.video_pipeline.transcript import Cue

logger = logging.getLogger(__name__)

MATCH = "match"
NO_MATCH = "no_match"

# Words carrying no identifying power. A title matching only on these would
# match near enough any stretch of a financial-aid webinar.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by for from how in into is it its of on or our
    that the their there these this to was what when where which who why will
    with you your
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9']+")

# Share of a title's content words that must appear nearby for a match. One
# word out of five is coincidence in a webinar that says "aid" every minute;
# half of them is the presenter reading the slide.
_MATCH_RATIO = 0.5


def content_words(text: str) -> set[str]:
    """Identifying words of a title — lowercased, stopwords and stubs removed."""
    return {
        word
        for word in _WORD_RE.findall(text.casefold())
        if len(word) > 2 and word not in _STOPWORDS
    }


def speech_near(cues: list[Cue], timecode: float, window: float) -> set[str]:
    """Every word spoken within ``window`` seconds either side of ``timecode``."""
    spoken: set[str] = set()
    for cue in cues:
        # Overlap, not containment: a cue straddling the boundary still counts.
        if cue.end < timecode - window or cue.start > timecode + window:
            continue
        spoken.update(_WORD_RE.findall(cue.text.casefold()))
    return spoken


def annotate(
    chapters: list[Chapter],
    cues: list[Cue],
    *,
    window: float | None = None,
) -> list[Chapter]:
    """Return ``chapters`` with ``confidence`` filled in on the title-card ones.

    Fixed-label segments are left alone: their titles come from config, not from
    the model, so there is nothing to confirm. With no transcript nothing is
    annotated at all — an empty flag means "not checked", not "suspect".
    """
    if not cues:
        return chapters

    seconds = settings.video_chapter_crosscheck_seconds if window is None else window
    out: list[Chapter] = []
    for chapter in chapters:
        if chapter.source != TITLE_CARD:
            out.append(chapter)
            continue

        wanted = content_words(chapter.title)
        if not wanted:
            # Nothing identifying to look for — a title of stopwords alone.
            out.append(chapter)
            continue

        spoken = speech_near(cues, chapter.timecode, seconds)
        hits = len(wanted & spoken)
        matched = hits >= max(1, round(len(wanted) * _MATCH_RATIO))
        out.append(replace(chapter, confidence=MATCH if matched else NO_MATCH))

    suspect = [c.title for c in out if c.confidence == NO_MATCH]
    if suspect:
        logger.info(
            "%d of %d title-card chapters were not confirmed by nearby speech: %s",
            len(suspect),
            sum(1 for c in out if c.source == TITLE_CARD),
            ", ".join(suspect),
        )
    return out
