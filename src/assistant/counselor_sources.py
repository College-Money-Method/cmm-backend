"""Counselor-only knowledge: email templates a counselor can adapt and send.

Both kinds are global (no school), admin-authored and already readable by any
counselor, so they are indexed with empty scope as ``template`` documents that
families never retrieve. Google-Docs-format communication templates keep their
text outside the app, so they are left out. Merge tags such as ``{{date}}`` stay
literal so the assistant can draft with them.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.assistant.chunking import Section, rich_text_sections
from src.assistant.sources import SourceDocument, _text_section
from src.communications.models import CommunicationTemplate
from src.workshops.models import Workshop, WorkshopEmailTemplate


def _template_document(source_id, title: str, sections: list[Section]) -> SourceDocument:
    return SourceDocument(
        "template", source_id, title, None, sections, for_family=False, for_counselor=True
    )


def communication_template_documents(db: Session) -> Iterator[SourceDocument]:
    query = (
        select(CommunicationTemplate)
        .where(CommunicationTemplate.is_active.is_(True), CommunicationTemplate.format != "google_docs")
        .order_by(CommunicationTemplate.sort_order, CommunicationTemplate.name)
    )
    for template in db.scalars(query):
        sections = [
            *_text_section("Subject", template.subject),
            *_text_section("When to use", template.description),
            *rich_text_sections(template.content, default_heading="Email"),
        ]
        yield _template_document(template.id, f"Email template: {template.name}", sections)


def workshop_email_template_documents(db: Session) -> Iterator[SourceDocument]:
    query = (
        select(WorkshopEmailTemplate, Workshop.name)
        .outerjoin(Workshop, Workshop.id == WorkshopEmailTemplate.workshop_id)
        .order_by(WorkshopEmailTemplate.name, WorkshopEmailTemplate.id)
    )
    for template, workshop_name in db.execute(query):
        kind = f"{template.type}, {workshop_name}" if workshop_name else template.type
        sections = [
            *_text_section("Subject", template.subject),
            *_text_section("Email", template.body),
        ]
        yield _template_document(template.id, f"Workshop email: {template.name} ({kind})", sections)
