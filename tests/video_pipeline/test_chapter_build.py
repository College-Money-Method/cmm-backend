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
    build_chapters,
    collapse_runs,
    find_qna_start,
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
