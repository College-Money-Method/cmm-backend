"""Turn classified frames into the chapter list published to Vimeo.

Slide sections name themselves from their title cards. The recurring segments —
introduction, resource center tour, Q&A — carry no card, so they are named from
fixed labels and located by position on the timeline. The labels are constants
rather than model output because a recurring segment should not get a different
name each week.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.config import settings
from src.video_pipeline.frame_classify import (
    BLANK,
    CONTENT_SLIDE,
    ERROR,
    SCREEN_SHARE_OTHER,
    SPEAKER,
    TITLE_CARD,
    Classified,
)
from src.video_pipeline.transcript import Cue

logger = logging.getLogger(__name__)

# Read from config at import so a recurring segment reads identically on every
# school's page, and so a rename is a deploy setting rather than a code change.
LABEL_INTRODUCTION = settings.video_chapter_label_introduction
LABEL_TOUR = settings.video_chapter_label_tour
LABEL_QNA = settings.video_chapter_label_qna

# A brief screen share is a detour; a long one is the tour.
TOUR_MIN_SECONDS = settings.video_tour_min_seconds
# How far back into the final slide section to look for the sentence that opens
# the Q&A. The camera cut lands on the sampling grid; the presenter announces
# the Q&A a little earlier, while the last slide is still up. Bounded so an
# early "we'll take questions at the end" cannot match.
QNA_LOOKBACK_SECONDS = settings.video_qna_lookback_seconds
# Vimeo's real ceiling is undocumented in what we checked, so cap defensively.
MAX_CHAPTERS = settings.video_max_chapters
# Sources that name a recurring segment rather than a slide section. Exempt from
# the minimum-length floor: a tour running straight into the Q&A is two real
# chapters two minutes apart.
RECURRING_SOURCES = frozenset({"intro", "tour", "qna", "qna_transcript"})
# Effectively no cap, for callers that want the full list first so they can
# record whether applying the cap actually dropped anything.
NO_CAP = 1_000_000

# Phrases that open a Q&A block. Missing one is cosmetic — the trailing-speaker
# rule still produces the chapter, just with a less precise start.
_QNA_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:let's|lets|we'll|now)\s+(?:jump|move|get|go)\s+(?:in)?to\s+"
        r"(?:the\s+)?(?:questions|q\s*(?:and|&)\s*a)",
        r"\b(?:take|answer|open\s+(?:it\s+)?up\s+(?:for|to))\s+"
        r"(?:some\s+|your\s+|a\s+few\s+)?questions",
        r"\bq\s*(?:and|&)\s*a\s+(?:time|session|portion)",
        r"\bany\s+questions\s+(?:that|you|we)\b",
        r"\bfirst\s+question\b",
    )
]


@dataclass(frozen=True)
class Run:
    """Consecutive frames sharing one type."""

    type: str
    start: float
    end: float
    heading: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Chapter:
    timecode: int  # whole seconds, as Vimeo's chapters API expects
    title: str
    source: str    # which rule produced it, for the admin view
    # Advisory result of the transcript cross-check, filled in by
    # `chapter_confidence`. Empty on the fixed-label segments, whose titles are
    # not model output and so have nothing to confirm.
    confidence: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "timecode": self.timecode,
            "title": self.title,
            "source": self.source,
            "confidence": self.confidence,
        }


def normalise_heading(heading: str) -> str:
    """Compare headings by their words, so re-read whitespace or a trailing
    space cannot split one section in two."""
    return " ".join(heading.split()).casefold()


def _same_section(current_type: str, current_heading: str, frame: Classified) -> bool:
    """Whether `frame` continues the run being accumulated.

    Type equality is enough for every type except `title_card`. Two consecutive
    speaker frames are one camera angle and belong together, but two consecutive
    title cards with different headings are two different sections: collapsing
    those on type alone merges the entire deck into a single chapter.
    """
    if frame.type != current_type:
        return False
    if current_type != TITLE_CARD:
        return True
    if not frame.heading:
        # Same card, heading came back blank on the re-read. Keep it in this run
        # rather than opening a chapter that has no title to show.
        return True
    return normalise_heading(frame.heading) == normalise_heading(current_heading)


def collapse_runs(frames: list[Classified], duration: float | None = None) -> list[Run]:
    """Group consecutive same-type frames into runs.

    Frames that failed classification are dropped first so a single unreadable
    frame cannot split one section into two.
    """
    usable = [frame for frame in frames if frame.type != ERROR]
    dropped = len(frames) - len(usable)
    if dropped:
        logger.info("ignoring %d unclassifiable frame(s) when building runs", dropped)
    if not usable:
        return []

    runs: list[Run] = []
    current_type = usable[0].type
    current_start = usable[0].timestamp
    current_heading = usable[0].heading

    for frame in usable[1:]:
        if _same_section(current_type, current_heading, frame):
            # Keep the first heading seen in the run; later frames of the same
            # card repeat it, and a blank would erase it.
            current_heading = current_heading or frame.heading
            continue
        runs.append(Run(current_type, current_start, frame.timestamp, current_heading))
        current_type = frame.type
        current_start = frame.timestamp
        current_heading = frame.heading

    end = duration if duration and duration > current_start else usable[-1].timestamp
    runs.append(Run(current_type, current_start, end, current_heading))
    return runs


def find_qna_start(cues: list[Cue], after: float) -> float | None:
    """First cue after `after` that reads like the start of a Q&A block.

    Cues are expected on the trimmed video's clock. `after` is a floor supplied
    by the caller, which keeps an early "we'll take questions at the end" from
    being mistaken for the Q&A itself.
    """
    for cue in cues:
        if cue.start <= after:
            continue
        if any(pattern.search(cue.text) for pattern in _QNA_PATTERNS):
            return cue.start
    return None


def build_chapters(
    frames: list[Classified],
    *,
    cues: list[Cue] | None = None,
    duration: float | None = None,
    tour_min_seconds: float = TOUR_MIN_SECONDS,
    max_chapters: int = MAX_CHAPTERS,
    min_section_seconds: float = 0.0,
) -> tuple[list[Chapter], list[Run]]:
    """Map classified frames onto chapters. Returns (chapters, runs)."""
    runs = collapse_runs(frames, duration)
    if not runs:
        return [], []

    title_positions = [i for i, run in enumerate(runs) if run.type == TITLE_CARD]
    first_title = title_positions[0] if title_positions else None
    last_title = title_positions[-1] if title_positions else None

    # A session with no slides at all still has shape: the shared screen is the
    # material, and a speaker run on either side of it plays the same role a
    # speaker run either side of the deck does. Without this anchor every
    # speaker run in such a recording is labelled the introduction, so a
    # walkthrough that returns to camera at the end gets a second chapter
    # called "Introduction" two thirds of the way in.
    share_positions = [i for i, run in enumerate(runs) if run.type == SCREEN_SHARE_OTHER]
    first_share = share_positions[0] if share_positions else None
    last_share = share_positions[-1] if share_positions else None

    chapters: list[Chapter] = []
    for i, run in enumerate(runs):
        if run.type == TITLE_CARD:
            title = run.heading.strip()
            if not title:
                # A title card whose text could not be read would otherwise
                # produce an untitled chapter.
                logger.warning("title card at %.1fs had no heading; skipped", run.start)
                continue
            chapters.append(Chapter(int(run.start), title, TITLE_CARD))
        elif run.type == SPEAKER:
            if first_title is not None and i < first_title:
                chapters.append(Chapter(int(run.start), LABEL_INTRODUCTION, "intro"))
            elif last_title is not None and i > last_title:
                chapters.append(Chapter(int(run.start), LABEL_QNA, "qna"))
            elif first_share is not None and i < first_share:
                chapters.append(Chapter(int(run.start), LABEL_INTRODUCTION, "intro"))
            elif last_share is not None and i > last_share:
                chapters.append(Chapter(int(run.start), LABEL_QNA, "qna"))
            elif first_title is None and first_share is None:
                # Nothing was ever shared — the whole thing is one talking-head
                # session, so the opening label covers all of it.
                chapters.append(Chapter(int(run.start), LABEL_INTRODUCTION, "intro"))
            # A speaker run between sections is a camera cut, not a new section.
        elif run.type == SCREEN_SHARE_OTHER:
            if run.duration >= tour_min_seconds:
                chapters.append(Chapter(int(run.start), LABEL_TOUR, "tour"))
        elif run.type in (CONTENT_SLIDE, BLANK):
            pass  # absorbed into whatever section precedes it

    # The transcript locates the Q&A more precisely than a camera switch does.
    if cues:
        # The look-back is bounded so an early "any questions so far?" during the
        # material cannot be mistaken for the Q&A proper. A recording with no
        # slides needs that bound just as much, so fall back to the last shared
        # screen before giving up and searching the whole transcript.
        anchor = last_title if last_title is not None else last_share
        if anchor is not None:
            last_run = runs[anchor]
            floor = max(last_run.start, last_run.end - QNA_LOOKBACK_SECONDS)
        else:
            floor = 0.0
        qna_ts = find_qna_start(cues, floor)
        if qna_ts is not None:
            chapters = [c for c in chapters if c.source != "qna"]
            chapters.append(Chapter(int(qna_ts), LABEL_QNA, "qna_transcript"))

    chapters.sort(key=lambda chapter: chapter.timecode)
    chapters = dedupe(chapters)

    # Vimeo expects the timeline to start at zero; the first chapter is the
    # opening section either way, so pull it back rather than risk a rejection.
    if chapters and chapters[0].timecode != 0:
        first = chapters[0]
        chapters[0] = Chapter(0, first.title, first.source)

    chapters = merge_short_sections(chapters, min_section_seconds)
    chapters, _ = apply_cap(chapters, max_chapters)
    return chapters, runs


def apply_cap(chapters: list[Chapter], max_chapters: int) -> tuple[list[Chapter], bool]:
    """Cut the list to the cap. Returns ``(chapters, truncated)``.

    Callable on its own because the job row records whether anything was
    dropped, and a length equal to the cap cannot tell a list that was cut short
    from one that happened to end there.
    """
    if len(chapters) <= max_chapters:
        return chapters, False
    logger.warning("truncating %d chapters to the %d-chapter cap", len(chapters), max_chapters)
    return chapters[:max_chapters], True


# No two chapters are this close together, whatever produced them. A title card
# reading "Resource Center Tour + Q&A" and the tour it announces land seconds
# apart and are one boundary, not two, so the recurring exemption below cannot
# reach this far down.
COINCIDENT_SECONDS = 30.0


def merge_short_sections(chapters: list[Chapter], min_seconds: float) -> list[Chapter]:
    """Fold a chapter back into the one above it when it starts too soon after.

    The deck drops a heading-only slide whenever the presenter changes emphasis,
    and nothing about the frame distinguishes that from a section divider — both
    are one line of text on a plain background. Length does: a section runs for
    many minutes, a change of emphasis for one or two.

    This is the crude version of the question the transcript answers properly,
    and it is only used when the transcript could not be read. Both recurring
    segments and the chapter at zero are exempt.
    """
    if min_seconds <= 0:
        return chapters
    kept: list[Chapter] = []
    for chapter in chapters:
        if not kept:
            kept.append(chapter)
            continue
        previous = kept[-1]
        gap = chapter.timecode - previous.timecode
        recurring = (
            chapter.source in RECURRING_SOURCES or previous.source in RECURRING_SOURCES
        )
        if gap < COINCIDENT_SECONDS or (not recurring and gap < min_seconds):
            logger.info(
                "folding %r at %ds into %r — %ds after it",
                chapter.title,
                chapter.timecode,
                previous.title,
                gap,
            )
            continue
        kept.append(chapter)
    return kept


def dedupe(chapters: list[Chapter]) -> list[Chapter]:
    """Drop repeated titles and colliding timecodes, keeping the earliest."""
    out: list[Chapter] = []
    for chapter in chapters:
        if out and out[-1].title == chapter.title:
            continue  # same section re-shown; the first timestamp is the real start
        if out and out[-1].timecode == chapter.timecode:
            continue  # two runs rounded onto the same second
        out.append(chapter)
    return out
