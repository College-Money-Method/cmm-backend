"""Unit tests for turning classified frames into a Vimeo chapter list.

These cover the paths a synthetic video cannot reach cheaply: the recurring
segments that carry no title card (introduction, resource centre tour, Q&A),
and the guardrails that keep a bad classification from producing a broken
chapter list. The invariant that matters most: one title card is one chapter.
A regression that merges sections is invisible in the frames themselves and
only shows up as a replay with a single chapter covering ninety minutes.
"""

import pytest

from src.video_pipeline.chapter_build import (
    LABEL_INTRODUCTION,
    LABEL_QNA,
    LABEL_TOUR,
    Chapter,
    build_chapters,
    collapse_runs,
    find_qna_start,
    merge_short_sections,
)
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


def frame(timestamp: float, type_: str, heading: str = "") -> Classified:
    return Classified(
        index=int(timestamp),
        timestamp=timestamp,
        file=f"frame_{int(timestamp):04d}.jpg",
        type=type_,
        heading=heading,
    )


def titles(chapters):
    return [(c.timecode, c.title) for c in chapters]


class TestCollapseRuns:
    def test_consecutive_title_cards_with_different_headings_are_separate_runs(self):
        """The regression that produced a one-chapter replay: collapsing on type
        alone merged every slide section into a single run."""
        frames = [
            frame(0, TITLE_CARD, "Paying For College"),
            frame(20, TITLE_CARD, "The Aid Formula"),
            frame(40, TITLE_CARD, "Next Steps"),
        ]
        runs = collapse_runs(frames, duration=60)
        assert [r.heading for r in runs] == [
            "Paying For College",
            "The Aid Formula",
            "Next Steps",
        ]
        assert [(r.start, r.end) for r in runs] == [(0, 20), (20, 40), (40, 60)]

    def test_repeated_same_heading_stays_one_run(self):
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, TITLE_CARD, "The Aid Formula"),
        ]
        assert len(collapse_runs(frames, duration=40)) == 1

    def test_heading_compared_by_words_not_whitespace(self):
        frames = [
            frame(0, TITLE_CARD, "The  Aid Formula"),
            frame(20, TITLE_CARD, "The Aid Formula "),
        ]
        assert len(collapse_runs(frames, duration=40)) == 1

    def test_blank_heading_does_not_open_a_new_run(self):
        """A re-read that came back without a heading is the same card, and
        opening a chapter there would give it no title to display."""
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, TITLE_CARD, ""),
        ]
        runs = collapse_runs(frames, duration=40)
        assert len(runs) == 1
        assert runs[0].heading == "The Aid Formula"

    def test_consecutive_speaker_frames_stay_one_run(self):
        """Camera cuts must not split a section."""
        frames = [frame(0, SPEAKER), frame(20, SPEAKER), frame(40, SPEAKER)]
        assert len(collapse_runs(frames, duration=60)) == 1

    def test_error_frames_dropped_before_grouping(self):
        frames = [
            frame(0, TITLE_CARD, "Intro Slide"),
            frame(20, ERROR),
            frame(40, TITLE_CARD, "Intro Slide"),
        ]
        runs = collapse_runs(frames, duration=60)
        assert len(runs) == 1, "an unreadable frame must not split one section"

    def test_all_errors_yields_no_runs(self):
        assert collapse_runs([frame(0, ERROR), frame(20, ERROR)], duration=40) == []

    def test_empty_input(self):
        assert collapse_runs([], duration=60) == []

    def test_duration_shorter_than_last_frame_falls_back_to_frame_time(self):
        frames = [frame(0, TITLE_CARD, "A Slide"), frame(100, TITLE_CARD, "B Slide")]
        runs = collapse_runs(frames, duration=50)
        assert runs[-1].end == 100


class TestFixedLabels:
    def test_speaker_before_first_card_is_introduction(self):
        frames = [frame(0, SPEAKER), frame(30, TITLE_CARD, "The Aid Formula")]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, LABEL_INTRODUCTION), (30, "The Aid Formula")]

    def test_speaker_after_last_card_is_qna(self):
        frames = [frame(0, TITLE_CARD, "The Aid Formula"), frame(30, SPEAKER)]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, "The Aid Formula"), (30, LABEL_QNA)]

    def test_speaker_between_cards_is_absorbed_as_a_camera_cut(self):
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, SPEAKER),
            frame(40, TITLE_CARD, "Next Steps"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, "The Aid Formula"), (40, "Next Steps")]

    def test_speaker_only_recording_is_a_single_introduction(self):
        frames = [frame(0, SPEAKER), frame(30, SPEAKER)]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, LABEL_INTRODUCTION)]

    def test_a_walkthrough_that_returns_to_camera_does_not_repeat_the_intro(self):
        """The shape of a real resource-centre walkthrough: talking head, a long
        shared screen, then back to camera. With no title card anywhere, every
        speaker run used to be labelled the introduction, so the closing
        discussion became a second "Introduction" two thirds of the way in.
        """
        frames = [
            frame(27, SPEAKER),
            frame(337, SCREEN_SHARE_OTHER),
            frame(1950, SPEAKER),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=2307, tour_min_seconds=120)
        assert titles(chapters) == [(0, LABEL_INTRODUCTION), (337, LABEL_TOUR), (1950, LABEL_QNA)]

    def test_the_share_anchors_the_qna_lookback_when_there_is_no_title_card(self):
        """The bounded look-back stops an early "any questions?" being read as the
        Q&A proper. Without a title card to anchor it the search would start at
        zero and match the aside during the tour."""
        frames = [
            frame(0, SPEAKER),
            frame(300, SCREEN_SHARE_OTHER),
            frame(1800, SPEAKER),
        ]
        cues = [
            Cue(start=600.0, end=604.0, text="any questions so far before I move on"),
            Cue(start=1810.0, end=1814.0, text="alright let us take your questions"),
        ]
        chapters, _ = build_chapters(frames, cues=cues, duration=2000, tour_min_seconds=120)
        qna = [c for c in chapters if c.title == LABEL_QNA]
        assert len(qna) == 1
        assert qna[0].timecode >= 1700, "the aside during the tour is not the Q&A"

    def test_long_screen_share_becomes_the_tour(self):
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(30, SCREEN_SHARE_OTHER),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=300, tour_min_seconds=120)
        assert titles(chapters) == [(0, "The Aid Formula"), (30, LABEL_TOUR)]

    def test_short_screen_share_is_absorbed(self):
        """A brief share is someone's stray window, not the resource centre."""
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(30, SCREEN_SHARE_OTHER),
            frame(50, TITLE_CARD, "Next Steps"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=80, tour_min_seconds=120)
        assert titles(chapters) == [(0, "The Aid Formula"), (50, "Next Steps")]

    def test_content_slide_and_blank_are_absorbed(self):
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, CONTENT_SLIDE),
            frame(40, BLANK),
            frame(60, TITLE_CARD, "Next Steps"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=80)
        assert titles(chapters) == [(0, "The Aid Formula"), (60, "Next Steps")]

    def test_title_card_with_empty_heading_is_skipped(self):
        """Better to omit a chapter than publish one with a blank title."""
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, CONTENT_SLIDE),
            frame(40, TITLE_CARD, ""),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, "The Aid Formula")]


class TestQnaFromTranscript:
    def test_transcript_match_moves_the_qna_start(self):
        """The speaker frame lands on a 2s sampling grid; the transcript knows
        the actual sentence where questions begin."""
        cues = [
            Cue(start=100.0, end=105.0, text="And that is the last of the slides."),
            Cue(start=106.0, end=112.0, text="Let's open it up for questions."),
        ]
        frames = [frame(0, TITLE_CARD, "The Aid Formula"), frame(120, SPEAKER)]
        chapters, _ = build_chapters(frames, cues=cues, duration=200)
        qna = [c for c in chapters if c.title == LABEL_QNA]
        assert len(qna) == 1
        assert qna[0].timecode == 106
        assert qna[0].source == "qna_transcript"

    def test_no_transcript_match_keeps_the_frame_derived_start(self):
        cues = [Cue(start=100.0, end=105.0, text="Here is another slide about assets.")]
        frames = [frame(0, TITLE_CARD, "The Aid Formula"), frame(120, SPEAKER)]
        chapters, _ = build_chapters(frames, cues=cues, duration=200)
        qna = [c for c in chapters if c.title == LABEL_QNA]
        assert qna[0].timecode == 120
        assert qna[0].source == "qna"

    def test_early_mention_during_the_opening_does_not_match(self):
        """The look-back into the final section must not reach the housekeeping
        line every webinar opens with."""
        cues = [
            Cue(start=20.0, end=26.0, text="We'll take some questions at the end."),
            Cue(start=30.0, end=36.0, text="Let's get into the first topic."),
        ]
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(600, TITLE_CARD, "Next Steps"),
            frame(1200, SPEAKER),
        ]
        chapters, _ = build_chapters(frames, cues=cues, duration=1400)
        qna = [c for c in chapters if c.title == LABEL_QNA]
        assert qna[0].timecode == 1200
        assert qna[0].source == "qna"

    def test_lookback_reaches_into_the_final_section(self):
        """The announcement lands while the last slide is still on screen."""
        cues = [Cue(start=1150.0, end=1158.0, text="Let's open it up for questions.")]
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(600, TITLE_CARD, "Next Steps"),
            frame(1200, SPEAKER),
        ]
        chapters, _ = build_chapters(frames, cues=cues, duration=1400)
        qna = [c for c in chapters if c.title == LABEL_QNA]
        assert qna[0].timecode == 1150
        assert qna[0].source == "qna_transcript"

    def test_find_qna_start_ignores_matches_before_the_cutoff(self):
        cues = [
            Cue(start=10.0, end=15.0, text="Put your questions in the Q and A box."),
            Cue(start=200.0, end=206.0, text="Let's take some questions."),
        ]
        assert find_qna_start(cues, after=100.0) == 200.0

    def test_find_qna_start_returns_none_without_a_match(self):
        cues = [Cue(start=200.0, end=205.0, text="Thanks everyone, good night.")]
        assert find_qna_start(cues, after=100.0) is None


class TestGuardrails:
    def test_first_chapter_is_forced_to_zero(self):
        """Vimeo expects the timeline to start at zero."""
        frames = [frame(8, TITLE_CARD, "The Aid Formula"), frame(40, TITLE_CARD, "Next Steps")]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert chapters[0].timecode == 0

    def test_repeated_consecutive_titles_are_deduped(self):
        frames = [
            frame(0, TITLE_CARD, "The Aid Formula"),
            frame(20, CONTENT_SLIDE),
            frame(40, TITLE_CARD, "The Aid Formula"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert titles(chapters) == [(0, "The Aid Formula")]

    def test_colliding_timecodes_are_deduped(self):
        """Two runs inside the same whole second cannot both be published."""
        frames = [
            frame(0.0, TITLE_CARD, "The Aid Formula"),
            frame(0.4, TITLE_CARD, "Next Steps"),
            frame(30.0, TITLE_CARD, "Wrapping Up"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert [c.timecode for c in chapters] == sorted({c.timecode for c in chapters})
        assert len(chapters) == 2

    def test_chapters_are_capped(self):
        frames = [frame(i * 20, TITLE_CARD, f"Slide Number {i}") for i in range(10)]
        chapters, _ = build_chapters(frames, cues=[], duration=200, max_chapters=4)
        assert len(chapters) == 4
        assert titles(chapters)[0] == (0, "Slide Number 0")

    def test_chapters_are_sorted_by_timecode(self):
        frames = [
            frame(40, TITLE_CARD, "Next Steps"),
            frame(0, TITLE_CARD, "The Aid Formula"),
        ]
        chapters, _ = build_chapters(frames, cues=[], duration=60)
        assert [c.timecode for c in chapters] == [0, 40]

    def test_no_frames_yields_no_chapters(self):
        chapters, runs = build_chapters([], cues=[], duration=60)
        assert chapters == []
        assert runs == []


class TestTheMinimumSectionLength:
    """The floor that stands in for the transcript when it could not be read.

    Nothing about a frame separates a section divider from a heading-only slide
    marking a change of emphasis — both are one line of text on a plain
    background. Length does: a section runs for many minutes.
    """

    def test_a_chapter_starting_too_soon_folds_into_the_one_above(self):
        chapters = [
            Chapter(0, "Preparing a Financial Plan", TITLE_CARD),
            Chapter(120, "Investment and Value", TITLE_CARD),
            Chapter(900, "Next Steps", TITLE_CARD),
        ]

        kept = merge_short_sections(chapters, 240.0)

        assert [c.timecode for c in kept] == [0, 900]

    def test_the_gap_is_measured_from_the_chapter_that_survived(self):
        """Otherwise a run of short hops keeps its last entry, which is the one
        furthest from where the section actually began."""
        chapters = [
            Chapter(0, "A", TITLE_CARD),
            Chapter(50, "B", TITLE_CARD),
            Chapter(100, "C", TITLE_CARD),
            Chapter(161, "D", TITLE_CARD),
        ]

        assert [c.timecode for c in merge_short_sections(chapters, 240.0)] == [0]

    def test_a_recurring_segment_is_exempt_on_either_side(self):
        """A tour running straight into the Q&A is two real chapters minutes
        apart, and both are what a viewer scrubs for."""
        chapters = [
            Chapter(0, LABEL_INTRODUCTION, "intro"),
            Chapter(3634, LABEL_TOUR, "tour"),
            Chapter(3797, LABEL_QNA, "qna"),
        ]

        assert merge_short_sections(chapters, 240.0) == chapters

    def test_a_recurring_segment_seconds_after_a_card_is_still_folded(self):
        """A card reading "Resource Center Tour + Q&A" and the tour it announces
        are one boundary. The exemption is for minutes apart, not seconds."""
        chapters = [
            Chapter(3634, "Resource Center Tour + Q&A", TITLE_CARD),
            Chapter(3639, LABEL_TOUR, "tour"),
            Chapter(3797, LABEL_QNA, "qna"),
        ]

        kept = merge_short_sections(chapters, 240.0)

        assert [(c.timecode, c.title) for c in kept] == [
            (3634, "Resource Center Tour + Q&A"),
            (3797, LABEL_QNA),
        ]

    def test_no_floor_leaves_the_list_alone(self):
        """The default, so every existing caller keeps its behaviour — including
        two chapters that no floor would let stand."""
        chapters = [Chapter(0, "A", TITLE_CARD), Chapter(10, "B", TITLE_CARD)]

        assert merge_short_sections(chapters, 0.0) == chapters

    def test_the_floor_reaches_the_chapters_a_build_produces(self):
        frames = [
            frame(0, TITLE_CARD, "Preparing a Financial Plan"),
            frame(120, TITLE_CARD, "Investment and Value"),
            frame(900, TITLE_CARD, "Next Steps"),
        ]

        chapters, _ = build_chapters(
            frames, cues=[], duration=1200, min_section_seconds=240.0
        )

        assert titles(chapters) == [(0, "Preparing a Financial Plan"), (900, "Next Steps")]
