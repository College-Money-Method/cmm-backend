"""The reviewable side of a reel's selection: its sentences, its warnings, a revision."""

from __future__ import annotations

import pytest

from src.video_pipeline import bedrock_usage, trailer_select
from src.video_pipeline.trailer_revise import describe_reel, revise_segments
from src.video_pipeline.trailer_select import Selection, SelectionError, rule_problems, validate
from src.video_pipeline.trailer_sentences import strip_speaker
from src.video_pipeline.transcript import Cue


def _lines(n: int, seconds: float = 6.0) -> list[Cue]:
    return [Cue(start=i * seconds, end=(i + 1) * seconds - 0.5,
                text=f"Paul Martin, CMM: Line {i}.") for i in range(n)]


def _answer(*ranges: tuple[int, int], **extra) -> dict:
    return {"hook_title": "Hook", **extra, "segments": [
        {"first_cue": a, "last_cue": b, "why": "", "flags": []} for a, b in ranges]}


@pytest.fixture
def model(monkeypatch):
    """Canned model answers, in order, and the calls that asked for them."""
    answers: list[dict] = []
    calls: list[dict] = []

    def call_json(**kw):
        calls.append(kw)
        return answers.pop(0), 100, 50

    monkeypatch.setattr(trailer_select, "call_json", call_json)
    monkeypatch.setattr(trailer_select.settings, "trailer_presenter_name", "Paul Martin")
    return answers, calls


def test_each_clip_keeps_its_sentences_without_the_speaker():
    selection = validate(_answer((10, 12), (20, 22), (30, 32)), _lines(100))
    first = selection.segments[0]
    assert [line["text"] for line in first.lines] == ["Line 10.", "Line 11.", "Line 12."]
    assert first.lines[0]["start"] == 60.0 and first.lines[-1]["end"] == 77.5
    assert Selection.from_dict(selection.as_dict()).segments[0].lines == first.lines


def test_strip_speaker_leaves_unlabelled_text_alone():
    assert strip_speaker("Jane Doe, Holy Names: A question?") == "A question?"
    assert strip_speaker("Just words.") == "Just words."
    assert strip_speaker("Deadline is 5:00 p.m.") == "Deadline is 5:00 p.m."


def test_a_selection_within_the_rules_has_no_problems():
    selection = validate(_answer((10, 12), (20, 22), (30, 32)), _lines(100))
    assert rule_problems(selection) == []


def test_a_reel_cut_down_by_hand_is_warned_about_both_rules():
    selection = validate(_answer((10, 12), (20, 22), (30, 32)), _lines(100))
    short = Selection(hook_title="Hook", segments=selection.segments[:1])
    problems = rule_problems(short)
    assert len(problems) == 2
    assert problems[0].startswith("1 clip —") and "at least 3" in problems[0]
    assert "at least 45s" in problems[1]


def test_describe_reel_numbers_clips_in_play_order():
    selection = validate(_answer((30, 32), (10, 12), (20, 22)), _lines(100))
    text = describe_reel(selection)
    assert text.startswith("Hook title: Hook\nClip 1: sentences [30]-[32]")
    assert "Clip 3: sentences [20]-[22]" in text and "Total:" in text


def test_a_revision_sees_the_reel_and_conversation_and_returns_the_reply(model):
    answers, calls = model
    cues = _lines(100)
    current = validate(_answer((10, 12), (20, 22), (30, 32)), cues)
    answers.append(_answer((10, 12), (40, 42), (30, 32), reply="Swapped clip 2."))

    selection, reply = revise_segments(
        cues, [], "Paying for College", current=current,
        conversation=[{"role": "admin", "text": "Focus on merit aid"},
                      {"role": "assistant", "text": "Here's a first cut"},
                      {"role": "edit", "text": "Changed the hook title"}],
        request="Swap clip 2 for something on deadlines", focus="Focus on merit aid")

    assert [s.first_cue for s in selection.segments] == [10, 40, 30]
    assert reply == "Swapped clip 2."
    prompt = calls[0]["content"]
    assert "Clip 2: sentences [20]-[22]" in prompt
    assert "Admin edited the reel: Changed the hook title" in prompt
    assert prompt.rstrip().endswith("The admin now asks: Swap clip 2 for something on deadlines")
    assert calls[0]["invoke_type"] == bedrock_usage.TRAILER_REVISE


def test_a_revision_that_breaks_a_rule_is_retried_with_the_reason(model):
    answers, calls = model
    cues = _lines(100)
    current = validate(_answer((10, 12), (20, 22), (30, 32)), cues)
    answers += [_answer((10, 11)), _answer((10, 12), (20, 22), (30, 32))]

    _selection, reply = revise_segments(cues, [], "T", current=current, conversation=[],
                                        request="Shorter")

    assert len(calls) == 2 and "previous answer was rejected" in calls[1]["content"]
    assert reply == "Updated the reel."


def test_a_revision_the_model_cannot_fit_raises(model):
    answers, _calls = model
    cues = _lines(100)
    current = validate(_answer((10, 12), (20, 22), (30, 32)), cues)
    answers += [_answer((10, 11)), _answer((10, 11))]
    with pytest.raises(SelectionError, match="no usable reel"):
        revise_segments(cues, [], "T", current=current, conversation=[], request="One clip")
