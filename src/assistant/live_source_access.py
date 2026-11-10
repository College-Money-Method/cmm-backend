"""Who may see a source right now, re-read by a writer that holds ``lock_source``.

A writer can read a source, spend seconds embedding it, and only then take the
lock to write — the nightly sync does exactly that. An admin who meanwhile
narrowed a resource's schools, made it counselor-only or unpublished it has
already patched the stored chunks under the same lock. Writing the copy read
earlier would undo that, so whoever writes a resource's chunks takes the
audience, link and scope from this read, made under the lock, instead of from
what it loaded before.

Only resources carry access that admins change in place; every other source's
access is fixed by its loader, so its document is already current.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.assistant.retrieval_scope import ChunkScope
from src.assistant.sources import SourceDocument, indexable_resource, resource_document
from src.content.models import ContentAsset

RESOURCE = "resource"


@dataclass(frozen=True)
class SourceAccess:
    for_family: bool
    for_counselor: bool
    url_path: str | None
    scope: ChunkScope


def live_resource_access(db: Session, asset_id: uuid.UUID) -> SourceAccess | None:
    """The resource's committed access, or None when it may no longer be indexed
    (unpublished, unapproved or deleted)."""
    # Pending changes in this session (an admin's own scope edit) must count too.
    db.flush()
    asset = db.scalar(
        select(ContentAsset)
        .where(ContentAsset.id == asset_id, indexable_resource())
        .execution_options(populate_existing=True)
    )
    if asset is None:
        return None
    # The scope relationships are viewonly and may already be loaded stale.
    db.expire(asset, ["schools", "cohorts", "state_rows"])
    doc = resource_document(asset)
    return SourceAccess(doc.for_family, doc.for_counselor, doc.url_path, doc.scope)


def live_access(db: Session, doc: SourceDocument) -> SourceAccess | None:
    if doc.source_type == RESOURCE:
        return live_resource_access(db, doc.source_id)
    return SourceAccess(doc.for_family, doc.for_counselor, doc.url_path, doc.scope)
