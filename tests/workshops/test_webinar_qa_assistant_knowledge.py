"""Which answered webinar questions reach the assistant, and in what form.

The rules gate what families are told, so each one is pinned here: a question
that slips through puts a presenter's off-the-cuff or one-family answer in front
of everyone, and one wrongly held back is knowledge the assistant never gets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.assistant.sources import _webinar_qa_documents
from src.assistant.webinar_qa_eligibility import scrub_contact_details, skip_reason
from src.workshops.models import Webinar, Workshop
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LONG_ANSWER = "Yes. Home equity on your primary residence counts on the CSS Profile at most schools."


def _question(**kwargs) -> WebinarQaQuestion:
    fields = {
        "id": uuid.uuid4(),
        "webinar_id": uuid.uuid4(),
        "zoom_question_id": str(uuid.uuid4()),
        "question_text": "Does home equity count?",
        "asked_at": NOW - timedelta(days=30),
        "answer_source": "typed",
        "typed_answer_text": LONG_ANSWER,
        "answer_visibility": "public",
        "classification": "question",
        "exclude_from_assistant": False,
        **kwargs,
    }
    return WebinarQaQuestion(**fields)


def _extraction(question: WebinarQaQuestion, confidence: str) -> WebinarQaAnswerExtraction:
    return WebinarQaAnswerExtraction(
        id=uuid.uuid4(),
        question=question,
        status="extracted",
        answer_text=LONG_ANSWER,
        confidence=Decimal(confidence),
        prompt_version="v1",
        created_at=NOW,
    )


# ── eligibility ──────────────────────────────────────────────────────────────


def test_an_answered_recent_question_is_used():
    assert skip_reason(_question(), NOW) is None


def test_an_unlabelled_question_is_used():
    # Labelling can fail; that must not silently drop real questions.
    assert skip_reason(_question(classification=None), NOW) is None


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"exclude_from_assistant": True}, "excluded"),
        ({"classification": "thanks"}, "not_a_question"),
        ({"typed_answer_text": None}, "unanswered"),
        ({"answer_visibility": "private"}, "private_answer"),
        # Zoom's pre-April-2026 format said nothing about visibility.
        ({"answer_visibility": None}, "unknown_visibility"),
        ({"typed_answer_text": "Yes, it does."}, "too_short"),
        ({"asked_at": NOW - timedelta(days=400)}, "too_old"),
        ({"asked_at": None}, "too_old"),
    ],
)
def test_each_rule_keeps_a_question_out(fields, reason):
    assert skip_reason(_question(**fields), NOW) == reason


def test_an_admin_relabel_decides_whether_it_is_a_question():
    assert skip_reason(_question(classification="comment", classification_override="question"), NOW) is None
    assert skip_reason(_question(classification_override="spam"), NOW) == "not_a_question"


def test_an_admins_answer_replaces_a_private_one():
    private = _question(answer_visibility="private", answer_text_override=LONG_ANSWER)
    assert skip_reason(private, NOW) is None


def test_a_recovered_answer_needs_the_model_to_be_sure():
    # Said out loud to the whole room, so visibility does not apply.
    sure = _question(typed_answer_text=None, answer_source="live", answer_visibility=None)
    _extraction(sure, "0.92")
    unsure = _question(typed_answer_text=None, answer_source="live")
    _extraction(unsure, "0.50")
    assert skip_reason(sure, NOW) is None
    assert skip_reason(unsure, NOW) == "low_confidence"


def test_contact_details_are_scrubbed():
    text = "Email jane.doe+aid@example.org or call (555) 123-4567 / +1 555.987.6543."
    assert scrub_contact_details(text) == "Email [email] or call [phone] / [phone]."
    # Dollar amounts and years are not phone numbers.
    assert scrub_contact_details("SAI under $7,395 for 2026-27") == "SAI under $7,395 for 2026-27"


def test_public_help_lines_are_kept():
    text = "Call Federal Student Aid at 1-800-433-3243."
    assert scrub_contact_details(text) == text


def test_the_askers_name_is_scrubbed():
    text = "Great question, Maria. Ms. Lopez-Diaz, your daughter Mariana qualifies."
    assert scrub_contact_details(text, "Maria Lopez-Diaz") == (
        "Great question, [name]. Ms. [name], your daughter Mariana qualifies."
    )


# ── one document per webinar ─────────────────────────────────────────────────


@pytest.fixture
def webinar_with_questions(qa_db):
    workshop = Workshop(id=uuid.uuid4(), name="Paying for College")
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        webinar_name="Paying for College _ Branson School, Menlo School",
        zoom_webinar_id="12345678901",
        start_datetime=datetime.now(UTC) - timedelta(days=20),
    )
    qa_db.add_all([workshop, webinar])
    qa_db.flush()
    asked = datetime.now(UTC) - timedelta(days=20)
    used = _question(
        webinar_id=webinar.id,
        question_text="Does   home equity\ncount?",
        asked_at=asked,
        asker_name="Dana Reed",
        typed_answer_text="Thanks Dana! " + LONG_ANSWER + " Questions? Write to aid@example.org.",
    )
    excluded = _question(webinar_id=webinar.id, asked_at=asked + timedelta(minutes=1), exclude_from_assistant=True)
    short = _question(webinar_id=webinar.id, asked_at=asked + timedelta(minutes=2), typed_answer_text="Yes.")
    qa_db.add_all([used, excluded, short])
    qa_db.commit()
    return webinar


def test_a_webinars_usable_answers_become_one_document(qa_db, webinar_with_questions):
    webinar = webinar_with_questions
    docs = list(_webinar_qa_documents(qa_db))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source_type == "webinar_qa"
    assert doc.source_id == webinar.id
    # Titled by topic; the session name lists the host schools.
    assert doc.title.startswith("Webinar Q&A: Paying for College (")
    assert doc.url_path is None
    # Only the usable question, whitespace tidied, contacts scrubbed, no asker.
    assert [s.text for s in doc.sections] == [
        f"Q: Does home equity count?\nA: Thanks [name]! {LONG_ANSWER} Questions? Write to [email]."
    ]
    assert "Dana" not in doc.sections[0].text


def test_a_webinar_with_nothing_usable_has_no_document(qa_db, webinar_with_questions):
    for question in qa_db.query(WebinarQaQuestion).all():
        question.exclude_from_assistant = True
    qa_db.commit()
    assert list(_webinar_qa_documents(qa_db)) == []
