"""A resource's access changes reach its chunks at once; the sync patches scope only.

Real sync and refresh code against SQLite; only Bedrock (enrichment and
embedding) is faked, and the fakes fail the test if a scope-only change calls
them. ``assistant_chunks`` is created without its generated tsvector, which
SQLite cannot compute and nothing here reads.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401  (registers every model)
from src.assistant import sync
from src.assistant.models import AssistantChunk
from src.assistant.resource_chunk_refresh import refresh_resource_chunks
from src.content import submissions_router
from src.content.models import (
    ContentAsset,
    ContentAssetSchool,
    ContentAssetState,
)
from src.content.schemas import SubmissionUpdate
from src.db.base import Base
from src.schools.models import School


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
          "communication_templates", "workshop_email_templates",
          # Deleting an asset clears its many-to-many rows.
          "content_asset_objectives", "workshop_resources")


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
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    keep = _closure(_SEEDS)
    for name, table in Base.metadata.tables.items():
        if name in keep:
            table.to_metadata(metadata)
    metadata.tables["webinars"].c.duration_minutes.computed = None
    metadata.tables["assistant_chunks"].c.search_vector.computed = None
    metadata.create_all(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("Bedrock must not be called")

    monkeypatch.setattr(sync, "enrich_chunk", forbidden)
    monkeypatch.setattr(sync, "embed_documents", forbidden)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _asset(db, **over) -> ContentAsset:
    fields = {"status": "published", "source": "cmm", **over}
    asset = ContentAsset(
        id=uuid.uuid4(), name="CSS Profile guide", description="How to fill in the CSS Profile.", **fields
    )
    db.add(asset)
    db.commit()
    return asset


def _chunk(asset: ContentAsset, **over) -> AssistantChunk:
    return AssistantChunk(
        source_type="resource", source_id=asset.id, chunk_index=0, title=asset.name, text="body",
        for_family=True, for_counselor=True, content_hash="h", embedding=[0.0] * 4,
        scope_school_ids=[], scope_cohort_ids=[], scope_states=[], **over,
    )


def _chunks(db) -> list[AssistantChunk]:
    db.expire_all()
    return list(db.scalars(select(AssistantChunk)))


def test_scope_edit_updates_chunks_in_the_same_transaction(db):
    asset = _asset(db)
    school = School(id=uuid.uuid4(), name="Branson")
    db.add_all([school, _chunk(asset)])
    db.commit()

    db.add(ContentAssetSchool(content_asset_id=asset.id, school_id=school.id))
    db.add(ContentAssetState(content_asset_id=asset.id, state="CA"))
    refresh_resource_chunks(db, asset.id)
    db.commit()
    [chunk] = _chunks(db)
    assert chunk.scope_school_ids == [str(school.id)]
    assert chunk.scope_states == ["CA"]

    db.query(ContentAssetSchool).filter_by(content_asset_id=asset.id).delete()
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db)[0].scope_school_ids == []


def test_unpublish_deletes_chunks(db):
    asset = _asset(db)
    db.add(_chunk(asset))
    db.commit()
    asset.status = "draft"
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db) == []


def test_unapproved_counselor_submission_is_not_kept(db):
    asset = _asset(db, source="counselor", review_status="pending_admin")
    db.add(_chunk(asset))
    db.commit()
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db) == []


def test_rejecting_an_approved_submission_deletes_its_chunks(db):
    asset = _asset(db, source="counselor", review_status="approved")
    db.add(_chunk(asset))
    db.commit()
    asset.review_status = "rejected"
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db) == []


def test_deleting_a_resource_deletes_its_chunks(db):
    asset = _asset(db)
    db.add(_chunk(asset))
    db.commit()
    db.delete(asset)
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db) == []


def test_audience_flag_flip_updates_flags_and_drops_the_public_link(db):
    asset = _asset(db)
    db.add(_chunk(asset, url_path=f"/resources/{asset.id}"))
    db.commit()
    asset.for_family = False
    refresh_resource_chunks(db, asset.id)
    db.commit()
    [chunk] = _chunks(db)
    assert (chunk.for_family, chunk.for_counselor, chunk.url_path) == (False, True, None)


def test_refresh_of_an_unindexed_resource_is_a_no_op(db):
    asset = _asset(db)
    refresh_resource_chunks(db, asset.id)
    db.commit()
    assert _chunks(db) == []


def test_sync_indexes_restricted_resource_then_patches_scope_without_embedding(db, monkeypatch):
    asset = _asset(db)
    db.add(ContentAssetState(content_asset_id=asset.id, state="TX"))
    db.commit()
    monkeypatch.setattr(sync, "enrich_chunk", lambda title, heading, text, audience: None)
    monkeypatch.setattr(sync, "embed_documents", lambda texts: [[0.0] * 4 for _ in texts])

    first = sync.sync_assistant_chunks(db)
    assert first.updated == 1 and first.rescoped == 0
    assert [c.scope_states for c in _chunks(db)] == [["TX"]]

    def forbidden(*args, **kwargs):
        raise AssertionError("a scope-only change must not call Bedrock")

    monkeypatch.setattr(sync, "enrich_chunk", forbidden)
    monkeypatch.setattr(sync, "embed_documents", forbidden)
    db.add(ContentAssetState(content_asset_id=asset.id, state="CA"))
    db.commit()
    db.expire_all()

    second = sync.sync_assistant_chunks(db)
    assert (second.rescoped, second.updated, second.unchanged) == (1, 0, 0)
    assert sorted(_chunks(db)[0].scope_states) == ["CA", "TX"]

    third = sync.sync_assistant_chunks(db)
    assert (third.rescoped, third.unchanged) == (0, 1)


def _embed_then(monkeypatch, admin_edit):
    """Fake Bedrock; ``admin_edit`` runs between the sync reading the resource
    and writing it, the window an admin's edit can land in."""
    monkeypatch.setattr(sync, "enrich_chunk", lambda title, heading, text, audience: None)

    def embed(texts):
        admin_edit()
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(sync, "embed_documents", embed)


def test_sync_keeps_a_scope_narrowed_while_it_was_embedding(db, monkeypatch):
    asset = _asset(db)
    school = School(id=uuid.uuid4(), name="Branson")
    db.add(school)
    db.commit()

    def narrow():
        db.add(ContentAssetSchool(content_asset_id=asset.id, school_id=school.id))
        db.add(ContentAssetState(content_asset_id=asset.id, state="TX"))

    _embed_then(monkeypatch, narrow)
    sync.sync_assistant_chunks(db)
    [chunk] = _chunks(db)
    assert (chunk.scope_school_ids, chunk.scope_states) == ([str(school.id)], ["TX"])


def test_sync_keeps_a_counselor_only_flip_made_while_it_was_embedding(db, monkeypatch):
    asset = _asset(db)

    def flip():
        asset.for_family = False

    _embed_then(monkeypatch, flip)
    sync.sync_assistant_chunks(db)
    [chunk] = _chunks(db)
    assert (chunk.for_family, chunk.url_path) == (False, None)


def test_sync_does_not_index_a_resource_unpublished_while_it_was_embedding(db, monkeypatch):
    asset = _asset(db)

    def unpublish():
        asset.status = "draft"

    _embed_then(monkeypatch, unpublish)
    result = sync.sync_assistant_chunks(db)
    assert _chunks(db) == [] and result.superseded == 1


def _approved_submission(db):
    counselor = SimpleNamespace(user_id=uuid.uuid4(), role="hub_user")
    asset = _asset(db, source="counselor", review_status="approved", submitted_by_id=counselor.user_id)
    db.add(_chunk(asset))
    db.commit()
    return asset, counselor


def test_editing_an_approved_submission_takes_it_out_of_the_index(db):
    asset, counselor = _approved_submission(db)
    submissions_router.update_submission(asset.id, SubmissionUpdate(name="Edited"), counselor, db)
    assert _chunks(db) == []


def test_deleting_an_approved_submission_deletes_its_chunks(db):
    asset, counselor = _approved_submission(db)
    submissions_router.delete_submission(asset.id, counselor, db)
    assert _chunks(db) == []
