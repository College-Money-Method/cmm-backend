"""The assistant's Bedrock spend lands in the shared ``bedrock_usage`` ledger,
which the admin Bedrock spend tab reads: answers (also when the family leaves midway)
and both kinds of embedding."""

from __future__ import annotations

import io
import json
import threading
import uuid
from types import SimpleNamespace

import pytest

from src.assistant import answer, embeddings
from src.config import settings
from src.video_pipeline import bedrock_usage
from tests.assistant.conftest import make_chunk


@pytest.fixture
def recorded(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(bedrock_usage, "record", lambda *args: rows.append(args))
    return rows


def _stream(**overrides):
    return answer.stream_answer(
        conversation_id=uuid.uuid4(), history=[], question="Who files?", chunks=[make_chunk()], profile={},
        system_prompt="prompt", **overrides,
    )


def test_a_full_answer_records_its_tokens(bedrock, recorded, monkeypatch):
    monkeypatch.setattr(answer, "_save_answer", lambda *a: uuid.uuid4())
    events = list(_stream())
    assert events[-1].startswith("event: done")
    assert recorded == [(bedrock_usage.ASSISTANT_ANSWER, settings.assistant_answer_model_id, 900, 40)]


@pytest.fixture
def saved(monkeypatch):
    rows: list[tuple] = []
    done = threading.Event()

    def save(*args):
        rows.append(args)
        done.set()
        return uuid.uuid4()

    monkeypatch.setattr(answer, "_save_answer", save)
    return SimpleNamespace(rows=rows, done=done)


def test_an_answer_left_midway_is_still_finished_saved_and_billed(bedrock, recorded, saved):
    stream = _stream()
    assert next(stream).startswith("event: meta")
    assert next(stream).startswith("event: delta")
    stream.close()  # what the server does when the family reloads or closes the tab
    assert saved.done.wait(2)
    assert saved.rows[0][1] == "Usually the parent the student lived with files [1]."
    assert saved.rows[0][4] == (900, 40)
    assert recorded == [(bedrock_usage.ASSISTANT_ANSWER, settings.assistant_answer_model_id, 900, 40)]


def test_a_crashed_answer_still_ends_the_stream_with_an_error(monkeypatch):
    def crash(*_):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this a generator

    monkeypatch.setattr(answer, "_answer_events", crash)
    events = list(_stream())
    assert len(events) == 1 and events[0].startswith("event: error")


def test_an_answer_that_never_started_records_nothing(bedrock, recorded):
    bedrock.fail = True
    assert list(_stream())[-1].startswith("event: error")
    assert recorded == []


class FakeEmbedClient:
    def __init__(self):
        self.calls = 0

    def invoke_model(self, body, **_):
        self.calls += 1
        texts = json.loads(body)["texts"]
        payload = {"embeddings": {"float": [[0.0] * 4 for _ in texts]}}
        return {
            "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": str(10 * len(texts))}},
            "body": io.BytesIO(json.dumps(payload).encode()),
        }


@pytest.fixture
def embed_client(monkeypatch):
    fake = FakeEmbedClient()
    monkeypatch.setattr(embeddings, "_client", lambda: fake)
    return fake


def test_a_question_embedding_is_recorded(embed_client, recorded):
    embeddings.embed_query("Who files?")
    assert recorded == [(bedrock_usage.ASSISTANT_QUESTION_EMBED, settings.assistant_embed_model_id, 10, 0)]


def test_content_embeddings_are_recorded_per_request(embed_client, recorded):
    embeddings.embed_documents(["chunk"] * 100)  # two requests: 96 + 4
    assert [r[0] for r in recorded] == [bedrock_usage.ASSISTANT_CONTENT_EMBED] * 2
    assert [r[2] for r in recorded] == [960, 40]


def test_embeddings_are_priced_at_the_embedding_rate():
    model = settings.assistant_embed_model_id
    expected = round(settings.assistant_embed_input_usd_per_mtok, 6)
    assert float(bedrock_usage.cost_usd(1_000_000, 0, model)) == expected
