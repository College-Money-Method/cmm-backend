"""Counselor templates are indexed counselor-only; audience shapes enrichment and links.

Real loaders and sync code against SQLite; only Bedrock is faked.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401  (registers every model)
from src.assistant import enrichment, sync
from src.assistant.counselor_sources import (
    communication_template_documents,
    workshop_email_template_documents,
)
from src.assistant.models import AssistantChunk
from src.assistant.retrieval_scope import ChunkScope
from src.assistant.sources import load_source_documents, resource_document
from src.communications.models import CommunicationTemplate
from src.content.models import ContentAsset
from src.db.base import Base
from src.workshops.models import Workshop, WorkshopEmailTemplate


@compiles(Vector, "sqlite")
def _vector_as_text(type_, compiler, **kw):
    return "TEXT"


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _tsvector_as_text(type_, compiler, **kw):
    return "TEXT"


_SEEDS = ("assistant_chunks", "content_assets", "content_asset_cohorts", "content_asset_states",
          "content_asset_schools", "topics", "faqs", "workshops", "webinar_qa_questions",
          "communication_templates", "workshop_email_templates")


def _closure(seed):
    needed, queue = set(), list(seed)
    while queue:
        name = queue.pop()
        table = Base.metadata.tables.get(name)
        if table is None or name in needed:
            continue
        needed.add(name)
        queue.extend(fk.column.table.name for fk in table.foreign_keys)
    return needed


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    keep = _closure(_SEEDS)
    for name, table in Base.metadata.tables.items():
        if name in keep:
            table.to_metadata(metadata)
    metadata.tables["webinars"].c.duration_minutes.computed = None
    metadata.tables["assistant_chunks"].c.search_vector.computed = None
    metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _tiptap(text: str) -> str:
    return json.dumps(
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
    )


def _communication(db, **over) -> CommunicationTemplate:
    fields = {"name": "FAFSA night invite", "format": "rich_text", "is_active": True,
              "subject": "Join us {{date}}", "description": "Send two weeks ahead",
              "content": _tiptap("Dear families, please join us."), **over}
    template = CommunicationTemplate(id=uuid.uuid4(), **fields)
    db.add(template)
    db.commit()
    return template


def test_active_rich_text_template_is_indexed_counselor_only(db):
    _communication(db)
    [doc] = list(communication_template_documents(db))
    assert doc.source_type == "template"
    assert doc.title == "Email template: FAFSA night invite"
    assert (doc.for_family, doc.for_counselor, doc.url_path) == (False, True, None)
    assert doc.scope == ChunkScope()
    text = "\n".join(s.text for s in doc.sections)
    assert "{{date}}" in text and "Send two weeks ahead" in text and "please join us" in text


def test_inactive_and_google_docs_templates_are_skipped(db):
    _communication(db, name="Retired", is_active=False)
    _communication(db, name="Doc", format="google_docs", content=None, google_docs_url="https://docs.google.com/x")
    assert list(communication_template_documents(db)) == []


def test_workshop_email_title_names_workshop_when_specific(db):
    workshop = Workshop(id=uuid.uuid4(), name="FAFSA Basics", sequence_number=1)
    db.add(workshop)
    db.flush()
    db.add_all([
        WorkshopEmailTemplate(id=uuid.uuid4(), workshop_id=workshop.id, type="announcement",
                              name="Announce", subject="S {{date}}", body="Line one\n\nLine two"),
        WorkshopEmailTemplate(id=uuid.uuid4(), workshop_id=None, type="followup",
                              name="Thanks", subject="S", body="Thank you"),
    ])
    db.commit()
    docs = {d.title: d for d in workshop_email_template_documents(db)}
    assert set(docs) == {
        "Workshop email: Announce (announcement, FAFSA Basics)",
        "Workshop email: Thanks (followup)",
    }
    assert all(d.for_family is False and d.for_counselor for d in docs.values())


def test_loader_includes_templates_and_sync_writes_counselor_only_chunks(db, monkeypatch):
    _communication(db)
    seen = []
    monkeypatch.setattr(sync, "enrich_chunk", lambda title, heading, text, audience: seen.append(audience))
    monkeypatch.setattr(sync, "embed_documents", lambda texts: [[0.0] * 4 for _ in texts])
    assert any(d.source_type == "template" for d in load_source_documents(db))

    sync.sync_assistant_chunks(db)
    chunks = list(db.scalars(select(AssistantChunk).where(AssistantChunk.source_type == "template")))
    assert chunks and all(not c.for_family and c.for_counselor for c in chunks)
    assert set(seen) == {"counselor"}


def test_counselor_only_resource_has_no_public_link(db):
    asset = ContentAsset(id=uuid.uuid4(), name="Note", description="Internal tip", status="published",
                         source="cmm", for_family=False, for_counselor=True)
    public = ContentAsset(id=uuid.uuid4(), name="Guide", description="Tip", status="published", source="cmm",
                          for_family=True, for_counselor=True)
    assert resource_document(asset).url_path is None
    assert resource_document(public).url_path == f"/resources/{public.id}"


@pytest.mark.parametrize("audience, expected", [("counselor", "counselor"), ("family", "parent or student")])
def test_enrichment_prompt_follows_audience(monkeypatch, audience, expected):
    captured = {}

    def fake_call_json(**kwargs):
        captured.update(kwargs)
        return {"questions": ["q1"]}, 0, 0

    monkeypatch.setattr(enrichment, "call_json", fake_call_json)
    assert enrichment.enrich_chunk("T", None, "text", audience=audience) == "q1"
    assert expected in captured["system"]
    assert ("counselor" in captured["system"]) == (audience == "counselor")


def test_patching_audience_flag_persists_and_reaches_chunks(db, monkeypatch):
    from src.content import router
    from src.content.router import update_asset
    from src.content.schemas import ContentAssetUpdate

    asset = ContentAsset(id=uuid.uuid4(), name="Note", description="Tip", status="published", source="cmm",
                         for_family=True, for_counselor=True)
    db.add(asset)
    db.add(AssistantChunk(
        source_type="resource", source_id=asset.id, chunk_index=0, title="Note", text="Tip",
        for_family=True, for_counselor=True, content_hash="h", embedding=[0.0] * 4,
        scope_school_ids=[], scope_cohort_ids=[], scope_states=[],
    ))
    db.commit()

    # The detail response joins many tables this SQLite schema omits; the write is what matters.
    monkeypatch.setattr(router, "_load_asset_detail", lambda db, asset_id: None)
    update_asset(asset.id, ContentAssetUpdate(for_family=False), None, db)
    db.expire_all()
    assert db.get(ContentAsset, asset.id).for_family is False
    [chunk] = db.scalars(select(AssistantChunk))
    assert (chunk.for_family, chunk.for_counselor) == (False, True)


def test_audience_flag_cannot_be_nulled():
    from pydantic import ValidationError

    from src.content.schemas import ContentAssetUpdate

    with pytest.raises(ValidationError):
        ContentAssetUpdate(for_family=None)
