"""Pure logic of the trailer reel: selection fitting, sentences, captions, words."""

from __future__ import annotations

import pytest

from src.video_pipeline.trailer_captions import LANDSCAPE, PORTRAIT, _ts, build_ass, group_words
from src.video_pipeline.trailer_edit import (
    CAMERA,
    SCREEN,
    join_graph,
    plan_shots,
    read_span,
    screen_windows,
)
from src.video_pipeline.trailer_select import Segment, SelectionError, presenter_mask, validate
from src.video_pipeline.trailer_sentences import speakers, split_sentences
from src.video_pipeline.trailer_words import Word, parse_items
from src.video_pipeline.transcript import Cue


def _lines(n: int, seconds: float = 6.0) -> list[Cue]:
    return [Cue(start=i * seconds, end=(i + 1) * seconds - 0.5, text=f"Line {i}.")
            for i in range(n)]


def _answer(*ranges: tuple[int, int]) -> dict:
    return {"hook_title": "Hook", "segments": [
        {"first_cue": a, "last_cue": b, "why": "", "flags": []} for a, b in ranges]}


def test_validate_keeps_segments_in_play_order():
    selection = validate(_answer((50, 52), (10, 12), (30, 32)), _lines(100))
    assert [s.first_cue for s in selection.segments] == [50, 10, 30]
    assert 45 <= selection.total_seconds <= 65


def test_validate_shortens_an_overlong_segment_to_whole_lines():
    selection = validate(_answer((0, 9), (20, 22), (40, 42)), _lines(100))
    first = selection.segments[0]
    assert first.first_cue == 0 and first.last_cue < 9
    assert first.duration <= 25


def test_validate_drops_overlaps_and_what_overflows_the_total():
    selection = validate(_answer((0, 2), (1, 3), (10, 12), (20, 22), (30, 32)), _lines(100))
    assert [s.first_cue for s in selection.segments] == [0, 10, 20]


def test_validate_rejects_too_little_with_every_reason():
    with pytest.raises(SelectionError) as exc:
        validate(_answer((0, 1), (500, 501)), _lines(100))
    assert "out of bounds" in str(exc.value) and "at least 3" in str(exc.value)


def test_validate_drops_a_segment_with_another_speaker():
    allowed = [True] * 100
    allowed[11] = False
    selection = validate(_answer((10, 12), (0, 2), (20, 22), (30, 32)), _lines(100), allowed)
    assert [s.first_cue for s in selection.segments] == [0, 20, 30]


def test_presenter_mask_carries_each_label_forward():
    sentences = [
        Cue(0, 1, "Paul Martin, College Money Method: Welcome."),
        Cue(1, 2, "Two."),
        Cue(2, 3, "Jane Doe, Holy Names: A question?"),
        Cue(3, 4, "paul martin, College Money Method: Answer."),
    ]
    assert speakers(sentences)[1] == "Paul Martin, College Money Method"
    assert presenter_mask(sentences, "Paul Martin") == [True, True, False, True]
    assert presenter_mask([Cue(0, 1, "No labels.")], "Paul Martin") == [True]


def test_split_sentences_across_cues_keeps_speaker_on_first_turn_only():
    cues = [
        Cue(0.0, 4.0, "Paul Martin, CMM: Effectively, financial aid"),
        Cue(4.0, 10.0, "Paul Martin, CMM: comes in two forms. One is need-based."),
        Cue(10.0, 12.0, "Jane Doe: Thanks!"),
    ]
    sentences = split_sentences(cues)
    assert [s.text for s in sentences] == [
        "Paul Martin, CMM: Effectively, financial aid comes in two forms.",
        "One is need-based.",
        "Jane Doe: Thanks!",
    ]
    assert sentences[0].start == 0.0 and 4.0 < sentences[0].end < 10.0
    assert sentences[1].end == pytest.approx(10.0)


def test_group_words_breaks_on_punctuation_pauses_and_size():
    words = [Word(0.0, 0.3, "One"), Word(0.3, 0.6, "is"), Word(0.6, 0.9, "need-based."),
             Word(0.9, 1.2, "Now"), Word(2.0, 2.3, "later")]
    groups = group_words(words, duration=3.0, layout=PORTRAIT)
    assert [[w.text for w in g.words] for g in groups] == [
        ["One", "is", "need-based."], ["Now"], ["later"]]
    assert groups[0].end == 0.9  # short gap bridged to the next group


def test_build_ass_writes_one_event_per_word_plus_cards_and_strip():
    words = [Word(0.0, 0.4, "Hello"), Word(0.4, 0.9, "there.")]
    script = build_ass(words, duration=10.0, layout=PORTRAIT, title="A {bad} title", cta="Watch",
                       presenter="Paul Martin · CMM")
    dialogues = [line for line in script.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 7  # 2 words, title, CTA, strip, progress bar, presenter
    assert "A (bad) title" in script  # override-tag braces neutralised
    assert "PlayResX: 1080" in script
    assert "Style: Caption,Inter ExtraBold," in script and "Style: Title,Lora," in script


def test_portrait_captions_move_into_the_band_on_screen_shots():
    words = [Word(0.0, 0.4, "Camera"), Word(5.0, 5.4, "screen")]

    def captions(layout):
        script = build_ass(words, duration=10.0, layout=layout, screen=[(4.0, 8.0)])
        return [line for line in script.splitlines() if ",Caption," in line]

    camera, screen = captions(PORTRAIT)
    assert "\\pos(" not in camera
    assert f"\\an5\\pos(540,{PORTRAIT.band_centre})" in screen
    # A word still showing when the picture switches is split there and moves.
    held = [Word(3.5, 4.5, "across")]
    before, after = [line for line in build_ass(held, duration=10.0, layout=PORTRAIT,
                                                 screen=[(4.0, 8.0)]).splitlines()
                     if ",Caption," in line]
    assert "0:00:04.00,Caption" in before and "\\pos(" not in before
    assert after.startswith("Dialogue: 0,0:00:04.00,") and "\\pos(" in after
    # Landscape screen shots keep the captions where they are.
    assert not any("\\pos(" in line for line in captions(LANDSCAPE))


def test_landscape_layout_fits_more_words_per_group():
    words = [Word(i * 0.3, i * 0.3 + 0.25, w) for i, w in enumerate("one two three four".split())]
    assert len(group_words(words, duration=2.0, layout=PORTRAIT)) == 2
    assert len(group_words(words, duration=2.0, layout=LANDSCAPE)) == 1
    assert "PlayResY: 1080" in build_ass(words, duration=2.0, layout=LANDSCAPE)


def test_screen_windows_skip_the_first_segment_and_hold_the_end_card():
    # Segment 1 stays on camera; each later one opens and closes on camera.
    assert screen_windows([16.0, 23.0, 15.0], hold_end=3.5) == [(18.0, 36.5), (41.0, 50.5)]
    assert screen_windows([16.0, 7.0, 15.0]) == [(25.0, 35.5)]  # 2.5 s visit is too short


def _shots():
    cuts = [Segment(0, 0, 100.0, 116.0, ""), Segment(1, 1, 300.0, 323.0, "")]
    return plan_shots(cuts, [(18.0, 36.5)], offset=8.0)


def test_plan_shots_puts_each_shot_on_its_recordings_clock():
    assert [(s.source, s.start, s.end, s.clock) for s in _shots()] == [
        (CAMERA, 0.0, 16.0, 108.0),
        (CAMERA, 16.0, 18.0, 308.0),
        (SCREEN, 18.0, 36.5, 310.0),
        (CAMERA, 36.5, 39.0, 328.5),
    ]


def test_read_span_adds_half_a_transition_either_side():
    shots = _shots()
    assert read_span(shots, 0, 30) == (108.0, 16.283)  # dissolve out: 0.25 s + a frame
    assert read_span(shots, 1, 30) == (307.75, 2.683)  # dissolve in, shrink out (0.4 s)
    assert read_span(shots, 3, 30) == (328.1, 2.9)  # the last shot has no tail


def test_join_graph_dissolves_between_segments_and_shrinks_to_the_screen():
    graph = join_graph(_shots(), ["p0", "p1", "p2", "p3"], "edit", size=(1280, 720),
                       camera_rect=(1066.67, 0.0, 213.33, 120.0))
    joins = graph.split(";[")
    assert "transition=fade:duration=0.5:offset=15.750[j1]" in joins[0]
    # Camera → screen: the camera's rectangle runs from the full frame to the thumbnail.
    assert "(0.00+1066.67*" in joins[1] and "a0(" in joins[1] and joins[1].endswith(
        "duration=0.8:offset=17.600[j2]")
    # Screen → camera: back out from the thumbnail, drawing the incoming camera.
    assert "(1066.67-1066.67*" in joins[2] and "b0(" in joins[2] and joins[2].endswith(
        "offset=36.100[edit]")


def test_join_graph_passes_a_single_shot_through():
    assert join_graph(_shots()[:1], ["p0"], "edit", size=(1280, 720),
                      camera_rect=(0, 0, 1, 1)) == "[p0]null[edit]"


def test_ts_formats_centiseconds():
    assert _ts(3725.456) == "1:02:05.46"


def test_parse_items_glues_punctuation():
    items = [
        {"type": "pronunciation", "start_time": "0.1", "end_time": "0.4",
         "alternatives": [{"content": "Hello"}]},
        {"type": "punctuation", "alternatives": [{"content": ","}]},
        {"type": "pronunciation", "start_time": "0.5", "end_time": "0.9",
         "alternatives": [{"content": "world"}]},
    ]
    assert [w.text for w in parse_items(items)] == ["Hello,", "world"]
