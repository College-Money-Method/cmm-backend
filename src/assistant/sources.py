"""Load the content the assistant may answer from, as heading-split documents.

Only content any family could already open goes in:

* topics with ``status='published'``;
* resources (``content_assets``) that are published, either CMM-authored or
  counselor submissions an admin approved, and carry no cohort/state/school
  restriction rows (restricted resources are school-specific and the chat
  site has no school context yet);
* workshops (no draft state; their descriptions are already public);
* FAQs.

Each document carries a ``content_hash`` over everything that ends up in its
chunks, so the sync job re-chunks a source only when something it says changed.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from src.assistant.chunking import Section, plain_text, rich_text_sections
from src.content.models import (
    ContentAsset,
    ContentAssetCohort,
    ContentAssetSchool,
    ContentAssetState,
    Faq,
    Topic,
)
from src.workshops.models import Workshop


@dataclass
class SourceDocument:
    source_type: str  # "topic" | "resource" | "workshop" | "faq"
    source_id: uuid.UUID
    title: str
    url_path: str | None
    sections: list[Section] = field(default_factory=list)
    for_family: bool = True
    for_counselor: bool = True

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [
                self.title,
                self.url_path,
                self.for_family,
                self.for_counselor,
                [[s.heading, s.text] for s in self.sections],
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def _text_section(heading: str | None, value) -> list[Section]:
    text = plain_text(value)
    return [Section(heading, text)] if text else []


def _list_section(heading: str, items) -> list[Section]:
    """Bullet list from a JSONB list of strings or Tiptap/text dicts."""
    lines = [f"- {line}" for item in items or [] if (line := plain_text(item))]
    return [Section(heading, "\n".join(lines))] if lines else []


def _topic_documents(db: Session) -> Iterator[SourceDocument]:
    for topic in db.scalars(select(Topic).where(Topic.status == "published").order_by(Topic.sort_order)):
        sections = [
            *_text_section(None, topic.description),
            *_text_section("Key points", topic.summary),
            *_list_section("Key points", topic.summary_items),
            *rich_text_sections(topic.content),
            *_list_section("What to do next", topic.action_items),
        ]
        yield SourceDocument("topic", topic.id, topic.title, f"/topic/{topic.slug}", sections)


def _resource_documents(db: Session) -> Iterator[SourceDocument]:
    restricted = or_(
        exists().where(ContentAssetCohort.content_asset_id == ContentAsset.id),
        exists().where(ContentAssetState.content_asset_id == ContentAsset.id),
        exists().where(ContentAssetSchool.content_asset_id == ContentAsset.id),
    )
    query = (
        select(ContentAsset)
        .where(
            ContentAsset.status == "published",
            or_(ContentAsset.source == "cmm", ContentAsset.review_status == "approved"),
            ~restricted,
        )
        .order_by(ContentAsset.name)
    )
    for asset in db.scalars(query):
        sections = [
            *_text_section(None, asset.description),
            *_text_section("What you'll learn", asset.summary),
            *_text_section("Why it matters", asset.why_important),
            *_text_section("How to use it", asset.how_to_use),
            *rich_text_sections(asset.content),
            *_list_section("What to do next", asset.action_items),
        ]
        yield SourceDocument(
            "resource",
            asset.id,
            asset.name,
            f"/resources/{asset.id}",
            sections,
            for_family=asset.for_family,
            for_counselor=asset.for_counselor,
        )


def _workshop_documents(db: Session) -> Iterator[SourceDocument]:
    for workshop in db.scalars(select(Workshop).order_by(Workshop.sequence_number)):
        sections = [
            *_text_section(None, workshop.description),
            *rich_text_sections(workshop.key_actions, default_heading="Key actions"),
            *_list_section("Key actions", workshop.key_action_items),
            *rich_text_sections(workshop.body),
            *_list_section("What to do next", workshop.action_items),
        ]
        # Workshops have no public page outside a school's hub, so no link.
        yield SourceDocument("workshop", workshop.id, f"Workshop: {workshop.name}", None, sections)


def _faq_documents(db: Session) -> Iterator[SourceDocument]:
    for faq in db.scalars(select(Faq)):
        question = plain_text(faq.question)
        if question:
            yield SourceDocument("faq", faq.id, question, None, _text_section(None, faq.answer))


def load_source_documents(db: Session) -> list[SourceDocument]:
    """Every answerable document, skipping ones with no text at all."""
    loaders = (_topic_documents, _resource_documents, _workshop_documents, _faq_documents)
    return [doc for load in loaders for doc in load(db) if doc.sections]
