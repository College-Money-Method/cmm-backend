"""An admin's change to a webinar's Q&A reaches the assistant's index at once.

Runs the real reindex against SQLite: only Bedrock (enrichment and embedding)
is faked. ``assistant_chunks`` is created without its generated tsvector, which
SQLite cannot compute and nothing here reads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData, select
from sqlalchemy.ext.compiler import compiles

from src.assistant import sync
from src.assistant.models import AssistantChunk
from src.assistant.webinar_qa_reindex import sync_webinar_qa_chunks
from src.db.base import Base
from src.workshops.models import Webinar, Workshop
from src.workshops.qa_models import WebinarQaQuestion

ANSWER = "Yes. Home equity on your primary residence counts on the CSS Profile at most schools."


@compiles(Vector, "sqlite")
def _compile_vector_as_text_on_sqlite(type_, compiler, **kw):
    return "TEXT"


@pytest.fixture
def db(qa_db, monkeypatch):
    metadata = MetaData()
    table = Base.metadata.tables["assistant_chunks"].to_metadata(metadata)
    table.c.search_vector.computed = None
    metadata.create_all(qa_db.get_bind())
    monkeypatch.setattr(sync, "enrich_chunk", lambda title, heading, text: None)
    monkeypatch.setattr(sync, "embed_documents", lambda texts: [[0.0] * 1024 for _ in texts])
    return qa_db


@pytest.fixture
def question(db):
    workshop = Workshop(id=uuid.uuid4(), name="Paying for College")
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        webinar_name="Paying for College _ Branson School",
        zoom_webinar_id="12345678901",
        start_datetime=datetime.now(UTC) - timedelta(days=20),
    )
    question = WebinarQaQuestion(
        id=uuid.uuid4(),
        webinar_id=webinar.id,
        zoom_question_id="q1",
        question_text="Does home equity count?",
        asked_at=datetime.now(UTC) - timedelta(days=20),
        answer_source="typed",
        typed_answer_text=ANSWER,
        answer_visibility="public",
        classification="question",
        exclude_from_assistant=False,
    )
    db.add_all([workshop, webinar, question])
    db.commit()
    return question


def _chunk_texts(db, webinar_id) -> list[str]:
    return list(db.scalars(select(AssistantChunk.text).where(AssistantChunk.source_id == webinar_id)))


def test_a_new_webinar_is_indexed_once(db, question):
    assert sync_webinar_qa_chunks(db, question.webinar_id) == "updated"
    assert _chunk_texts(db, question.webinar_id) == [f"Q: Does home equity count?\nA: {ANSWER}"]
    # The same hash as the nightly sync, so a second run does no Bedrock work.
    assert sync_webinar_qa_chunks(db, question.webinar_id) == "unchanged"


def test_a_corrected_answer_replaces_the_old_chunks(db, question):
    sync_webinar_qa_chunks(db, question.webinar_id)
    question.answer_text_override = "It depends on the school: most CSS Profile colleges count home equity, some cap it."
    db.commit()

    assert sync_webinar_qa_chunks(db, question.webinar_id) == "updated"
    [text] = _chunk_texts(db, question.webinar_id)
    assert "some cap it" in text


def test_excluding_the_last_usable_answer_removes_the_webinar(db, question):
    sync_webinar_qa_chunks(db, question.webinar_id)
    question.exclude_from_assistant = True
    db.commit()

    assert sync_webinar_qa_chunks(db, question.webinar_id) == "removed"
    assert _chunk_texts(db, question.webinar_id) == []
    assert sync_webinar_qa_chunks(db, question.webinar_id) == "unchanged"
