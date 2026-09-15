"""Zoom caps a custom-question answer at 128 characters.

The bug this pins down: a parent who wrote more than that in "Are there any
questions you would like to submit on this workshop topic?" had their whole
Zoom registration rejected with ``Invalid parameter: custom_questions``. The
failure was non-fatal, so their registration row committed as approved and they
simply never received a join link. 52 production registrations were stranded
this way — the parents who wrote the most were the ones silently dropped.
"""

from __future__ import annotations

from src.integrations.zoom import _ZOOM_ANSWER_MAX_CHARS, _match_answer

GRADES = ["9th", "10th", "11th", "12th"]


def test_free_text_answer_is_capped():
    answer = _match_answer("q" * 400, [])
    assert answer is not None
    assert len(answer) == _ZOOM_ANSWER_MAX_CHARS


def test_free_text_answer_within_the_limit_is_untouched():
    """The 98% of parents who write a normal-length question must be unaffected."""
    value = "How do we report a 529 plan on the FAFSA?"
    assert _match_answer(value, []) == value


def test_answer_at_the_limit_is_untouched():
    value = "x" * _ZOOM_ANSWER_MAX_CHARS
    assert _match_answer(value, []) == value


def test_dropdown_answers_are_returned_verbatim():
    """Capping must not corrupt a value Zoom matches against a fixed list."""
    assert _match_answer("12th", GRADES) == "12th"


def test_unmatched_dropdown_answer_is_still_none():
    assert _match_answer("Kindergarten", GRADES) is None
