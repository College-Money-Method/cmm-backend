"""What the Zoom Q&A pull must get right.

The sync is the only writer of ingested fact, and it runs again every time a
webhook retries, so two properties matter more than the parsing: it never
duplicates a question, and it never touches a column an admin can edit.
"""

from __future__ import annotations

from sqlalchemy import select

from src.integrations import zoom as zoom_client
from src.workshops.qa_models import WebinarQaQuestion, WebinarQaSync
from src.workshops import qa_sync_service
from src.workshops.qa_sync_service import sync_webinar_qa

# The shape Zoom's report actually has: questions grouped by asker, one group
# holding every anonymous submission with the literal string "anonymous" in all
# three identity fields.
REPORT = {
    "id": 83822890565,
    "uuid": "abc==",
    "start_time": "2026-09-15T23:22:37Z",
    "questions": [
        {
            "name": "Dana Reed",
            "email": "dana@example.edu",
            "user_id": "u-1",
            "question_details": [
                {
                    "question_id": "q-1",
                    "question": "Does the CSS Profile count home equity?",
                    "create_time": "2026-09-16T00:05:00Z",
                    "question_status": "answered",
                    "answer": "It depends on the school.",
                    "answer_details": [
                        {
                            "content": "It depends on the school.",
                            "type": "host_answered_publicly",
                            "name": "Paul Martin",
                            "email": "paul@collegemoneymethod.com",
                            "create_time": "2026-09-16T00:06:00Z",
                        }
                    ],
                },
                {
                    "question_id": "q-2",
                    "question": "What about a second home?",
                    "create_time": "2026-09-16T00:07:00Z",
                    "question_status": "answered",
                    "answer": "Live answered",
                    "answer_details": [
                        {
                            "content": "Live answered",
                            "type": "host_answered_privately",
                            "name": "Paul Martin",
                            "email": "paul@collegemoneymethod.com",
                            "create_time": "2026-09-16T00:08:00Z",
                        }
                    ],
                },
            ],
        },
        {
            "name": "anonymous",
            "email": "anonymous",
            "user_id": "anonymous",
            "question_details": [
                {
                    "question_id": "q-3",
                    "question": "Can I appeal an award letter?",
                    "create_time": "2026-09-16T00:09:00Z",
                    "question_status": "open",
                    "answer": "",
                    "answer_details": [],
                },
                {
                    "question_id": "q-4",
                    "question": "How late can I file?",
                    "create_time": "2026-09-16T00:10:00Z",
                    "question_status": "open",
                    "answer": "",
                    "answer_details": [],
                },
            ],
        },
    ],
}


def _no_classification(monkeypatch):
    """Labelling reaches Bedrock; the sync's own behaviour is what is under test."""
    monkeypatch.setattr(qa_sync_service, "classify_questions", lambda db, questions: 0)


def _report(monkeypatch, payload):
    monkeypatch.setattr(zoom_client, "get_webinar_qa", lambda zoom_webinar_id: payload)


def test_every_question_in_every_group_is_stored(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)

    assert sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db) is True

    rows = qa_db.scalars(select(WebinarQaQuestion)).all()
    assert {r.zoom_question_id for r in rows} == {"q-1", "q-2", "q-3", "q-4"}
    sync = qa_db.scalars(select(WebinarQaSync)).one()
    assert (sync.status, sync.question_count) == ("ok", 4)


def test_the_anonymous_group_becomes_real_nulls(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)
    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)

    anon = qa_db.scalars(
        select(WebinarQaQuestion).where(WebinarQaQuestion.zoom_question_id == "q-3")
    ).one()
    # Zoom puts the word "anonymous" in the email field. Stored as-is it would
    # be offered to the registration matcher and shown as an address.
    assert (anon.asker_email, anon.asker_name, anon.asker_zoom_user_id) == (None, None, None)
    assert anon.is_anonymous is True
    assert anon.registration_id is None


def test_the_answer_columns_say_where_the_answer_lives(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)
    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)

    by_id = {
        r.zoom_question_id: r for r in qa_db.scalars(select(WebinarQaQuestion)).all()
    }
    typed = by_id["q-1"]
    assert (typed.answer_source, typed.answer_visibility) == ("typed", "public")
    assert typed.typed_answer_text == "It depends on the school."

    # "Live answered" is a sentinel, not an answer — storing it as answer text
    # would show the admin the word instead of the missing answer.
    live = by_id["q-2"]
    assert (live.answer_source, live.typed_answer_text) == ("live", None)
    assert live.answer_visibility == "private"

    assert by_id["q-3"].answer_source == "unanswered"


def test_syncing_twice_does_not_duplicate_a_question(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)

    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)
    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)

    assert len(qa_db.scalars(select(WebinarQaQuestion)).all()) == 4
    assert len(qa_db.scalars(select(WebinarQaSync)).all()) == 2


def test_a_resync_leaves_an_admin_edit_alone(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)
    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)

    edited = qa_db.scalars(
        select(WebinarQaQuestion).where(WebinarQaQuestion.zoom_question_id == "q-1")
    ).one()
    edited.answer_text_override = "Only schools that ask for it."
    edited.classification_override = "comment"
    qa_db.commit()

    sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db)

    qa_db.expire_all()
    again = qa_db.scalars(
        select(WebinarQaQuestion).where(WebinarQaQuestion.zoom_question_id == "q-1")
    ).one()
    assert again.answer_text_override == "Only schools that ask for it."
    assert again.classification_override == "comment"
    # The ingested side still tracks Zoom.
    assert again.typed_answer_text == "It depends on the school."


def test_a_report_zoom_has_not_produced_is_recorded_not_lost(qa_db, qa_webinar, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, None)

    assert sync_webinar_qa(qa_webinar.zoom_webinar_id, qa_db) is False

    sync = qa_db.scalars(select(WebinarQaSync)).one()
    assert sync.status == "failed"
    assert sync.error_text
    # Without the row there is no way to tell a webinar nobody asked anything at
    # from one whose report never arrived.
    assert qa_db.scalars(select(WebinarQaQuestion)).all() == []


def test_an_unknown_webinar_is_refused_before_anything_is_written(qa_db, monkeypatch):
    _no_classification(monkeypatch)
    _report(monkeypatch, REPORT)

    assert sync_webinar_qa("99999999999", qa_db) is False
    assert qa_db.scalars(select(WebinarQaSync)).all() == []
