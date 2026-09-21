"""Telling genuine questions apart from the chatter in the same panel.

The rule pass is a short-circuit for bare pleasantries; the model does the real
work. What matters here is that neither can damage the sync it runs inside — a
Bedrock outage must leave the questions alone, and a label nobody recognises
must never reach the column the admin's default view filters on.
"""

from __future__ import annotations

import uuid

import pytest

from src.video_pipeline.bedrock_client import BedrockCallError
from src.workshops import qa_classification_service
from src.workshops.qa_classification_service import CHUNK_SIZE, NOISE_SYS, classify_questions
from src.workshops.qa_models import WebinarQaQuestion


@pytest.fixture
def make_question(qa_db, qa_webinar):
    counter = iter(range(1000))

    def _make(text: str, **kwargs) -> WebinarQaQuestion:
        question = WebinarQaQuestion(
            id=uuid.uuid4(),
            webinar_id=qa_webinar.id,
            zoom_question_id=f"q-{next(counter)}",
            question_text=text,
            **kwargs,
        )
        qa_db.add(question)
        qa_db.commit()
        return question

    return _make


def _model(monkeypatch, results):
    calls = []

    def _call_json(*, system, content, max_tokens, invoke_type):
        calls.append(content)
        return {"results": results}, 10, 20

    monkeypatch.setattr(qa_classification_service, "call_json", _call_json)
    return calls


@pytest.mark.parametrize(
    "text,label",
    [
        ("Thanks!", "thanks"),
        ("thank you", "thanks"),
        ("Hi everyone", "greeting"),
        ("Good morning!", "greeting"),
    ],
)
def test_a_bare_pleasantry_never_reaches_the_model(qa_db, make_question, monkeypatch, text, label):
    question = make_question(text)
    calls = _model(monkeypatch, [])

    assert classify_questions(qa_db, [question]) == 1
    assert (question.classification, question.classified_by) == (label, "rule")
    assert calls == []


def test_a_pleasantry_with_a_question_after_it_goes_to_the_model(
    qa_db, make_question, monkeypatch
):
    # The rule patterns are anchored precisely so this does not short-circuit:
    # "Hi! Quick question ..." is a question.
    question = make_question("Hi! Quick question — does the CSS Profile count home equity?")
    _model(monkeypatch, [{"i": 0, "label": "question"}])

    assert classify_questions(qa_db, [question]) == 1
    assert (question.classification, question.classified_by) == ("question", "llm")


def test_a_long_friendly_note_is_noise_when_the_model_says_so(
    qa_db, make_question, monkeypatch
):
    # Length is not evidence of a question. This is the case the cheap regex
    # pass cannot catch and the prompt is worded around.
    note = make_question(
        "Hello Paul, this is Dana from the counselling office across town — we send "
        "families to your sessions every year and they always come back grateful. "
        "Keep up the wonderful work!"
    )
    _model(monkeypatch, [{"i": 0, "label": "comment"}])

    classify_questions(qa_db, [note])
    assert note.classification == "comment"


def test_a_label_we_do_not_recognise_is_dropped_rather_than_stored(
    qa_db, make_question, monkeypatch
):
    question = make_question("Can I appeal an award letter?")
    _model(monkeypatch, [{"i": 0, "label": "kwestion"}])

    assert classify_questions(qa_db, [question]) == 0
    # Null reads as "unclassified" and is shown. A typo'd verdict in the column
    # would quietly filter a real question out of the admin's default view.
    assert question.classification is None


def test_a_bedrock_outage_keeps_the_rule_labels_and_raises_nothing(
    qa_db, make_question, monkeypatch
):
    thanks = make_question("Thanks!")
    real = make_question("How late can I file?")

    def _boom(**_kwargs):
        raise BedrockCallError("bedrock unavailable")

    monkeypatch.setattr(qa_classification_service, "call_json", _boom)

    assert classify_questions(qa_db, [thanks, real]) == 1
    assert thanks.classification == "thanks"
    assert real.classification is None


def test_an_already_labelled_question_is_left_alone(qa_db, make_question, monkeypatch):
    done = make_question("Can I appeal?", classification="question", classified_by="llm")
    calls = _model(monkeypatch, [])

    assert classify_questions(qa_db, [done]) == 0
    assert calls == []


def test_a_batch_inside_the_chunk_size_is_a_single_call(qa_db, make_question, monkeypatch):
    questions = [make_question(f"Question number {i}?") for i in range(5)]
    calls = _model(
        monkeypatch, [{"i": i, "label": "question"} for i in range(5)]
    )

    assert classify_questions(qa_db, questions) == 5
    # Per-question calls would be slower and dearer for no gain in accuracy.
    assert len(calls) == 1
    assert all(q.question_text in calls[0] for q in questions)


def test_a_webinar_larger_than_a_chunk_is_split(qa_db, make_question, monkeypatch):
    # The reply carries one object per submission, so the output cap — not the
    # input — is what a big webinar hits.
    questions = [make_question(f"Question number {i}?") for i in range(CHUNK_SIZE + 10)]
    calls = _model(monkeypatch, [{"i": i, "label": "question"} for i in range(CHUNK_SIZE)])

    assert classify_questions(qa_db, questions) == CHUNK_SIZE + 10
    assert len(calls) == 2


def test_a_chunk_the_model_fails_on_does_not_cost_the_rest(qa_db, make_question, monkeypatch):
    # This is the regression: one truncated reply used to abandon the whole
    # webinar, leaving every row unlabelled however many chunks were still fine.
    questions = [make_question(f"Question number {i}?") for i in range(CHUNK_SIZE + 10)]
    calls = []

    def _call_json(*, system, content, max_tokens, invoke_type):
        calls.append(content)
        if len(calls) == 1:
            raise BedrockCallError("Bedrock response was not valid JSON")
        return {"results": [{"i": i, "label": "question"} for i in range(CHUNK_SIZE)]}, 10, 20

    monkeypatch.setattr(qa_classification_service, "call_json", _call_json)

    assert classify_questions(qa_db, questions) == 10
    assert len(calls) == 2
    # The lost chunk stays null, which the API shows rather than hides.
    assert all(q.classification is None for q in questions[:CHUNK_SIZE])
    assert all(q.classification == "question" for q in questions[CHUNK_SIZE:])


def test_the_prompt_still_carries_the_rules_bought_with_real_misclassifications():
    """Pins wording, because every other test here mocks the model away.

    Both rules come from rows that were labelled wrongly on live data:

    * "Sorry, just joined. Is the webinar recorded? If yes, can we have the
      recording?" was stored as `greeting`, and "Hi. Thanks for the excellent
      presentation. What do you recommend for international students?" as
      `thanks`. Attendees are polite, so the pleasantry is usually first and
      the question follows it.
    * "Will there be a recording available?" and "Are all parents
      automatically on mute?" were stored as `comment` — not about the subject
      matter, so read as needing no answer. They need one more than most.

    Trimming either sentence puts those rows back in the noise pile, where the
    admin's default view never shows them.
    """
    assert "Judge the whole submission, not how it opens." in NOISE_SYS
    assert "asks for anything at all" in NOISE_SYS
    assert "are questions, not comments" in NOISE_SYS
    # The guard in the other direction: without it a colleague's long, warm
    # greeting reads as a question.
    assert "Long text is not automatically a question" in NOISE_SYS
