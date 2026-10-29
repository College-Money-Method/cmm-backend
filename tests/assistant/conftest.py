"""Shared harness for the assistant tests: in-memory SQLite with the chat tables.

``assistant_chunks`` (pgvector + generated tsvector) cannot exist in SQLite, so
retrieval is swapped for a fixed chunk list and Bedrock for a scripted stream.
The streamed answer saves through ``get_session_factory``, which is pointed at
the same in-memory engine.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401  (registers every model so relationship strings resolve)
from src.assistant import answer, router
from src.assistant.search import RetrievedChunk
from src.db.base import Base
from src.video_pipeline import bedrock_usage

TABLES = ("app_config", "assistant_conversations", "assistant_messages")


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # Deleting a conversation relies on the database's ON DELETE CASCADE, as in Postgres.
    event.listen(eng, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(eng, tables=[Base.metadata.tables[n] for n in TABLES])
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(factory):
    session = factory()
    try:
        yield session
    finally:
        session.close()


def make_chunk(title="Divorced and Separated Parents", heading="Who files", url="/topic/divorce") -> RetrievedChunk:
    return RetrievedChunk(
        id=uuid.uuid4(), source_type="topic", source_id=uuid.uuid4(), title=title, heading=heading,
        url_path=url, text="The parent the student lived with most files the FAFSA.", score=0.03,
    )


class FakeStream:
    def __init__(self, parts, fail=False):
        self._parts, self._fail = parts, fail

    def __enter__(self):
        if self._fail:
            raise RuntimeError("bedrock down")
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        yield from self._parts

    def get_final_message(self):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=900, output_tokens=40))


class FakeBedrock:
    """Records each ``messages.stream`` call and replays the scripted parts."""

    def __init__(self):
        self.parts = ["Usually the parent ", "the student lived with files [1]."]
        self.fail = False
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.parts, self.fail)


@pytest.fixture
def bedrock(monkeypatch, factory):
    fake = FakeBedrock()
    monkeypatch.setattr(answer, "get_client", lambda: fake)
    monkeypatch.setattr(answer, "get_session_factory", lambda: factory)
    monkeypatch.setattr(bedrock_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(router, "embed_query", lambda text: [0.0] * 1024)
    monkeypatch.setattr(router, "hybrid_search", lambda db, text, vector, role="family": [make_chunk()])
    monkeypatch.setattr(router, "BAD_PASSWORD_DELAY_SECONDS", 0)
    return fake
