"""Apply an admin's change to a resource to its chunks in the same transaction.

The nightly sync would catch up within a day, but a school removed from a
resource's scope, or a resource unpublished or deleted, must stop being
quoted at once, and a restricted resource must never be quoted to the wrong
school in the meantime. ``refresh_resource_chunks`` therefore runs inside the
content endpoint's own transaction, just before its commit: if the endpoint
fails and rolls back, the chunks roll back with it and never get ahead of the
source.

Invariant: every code path that changes a resource's publication, review
state, audience flags or school/cohort/state scope calls this before commit.

What it patches in place: the audience flags, the link and the scope. The
flags and link stay in the content hash, so the next sync re-embeds that one
resource (cents); the scope is outside the hash and needs no embedding at all.
Edits to the text itself are left to the sync.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.assistant.live_source_access import RESOURCE, live_resource_access
from src.assistant.models import AssistantChunk
from src.assistant.sync import delete_source, lock_source, set_source_scope


def refresh_resource_chunks(db: Session, asset_id: uuid.UUID) -> None:
    """Delete the resource's chunks if it is no longer indexable, else patch
    their flags, link and scope. Does not commit; the caller does. On delete,
    call it after ``db.delete(asset)`` so the read below sees it gone."""
    lock_source(db, RESOURCE, asset_id)
    access = live_resource_access(db, asset_id)
    if access is None:
        delete_source(db, RESOURCE, asset_id)
        return
    db.execute(
        update(AssistantChunk)
        .where(AssistantChunk.source_type == RESOURCE, AssistantChunk.source_id == asset_id)
        .values(for_family=access.for_family, for_counselor=access.for_counselor, url_path=access.url_path)
    )
    set_source_scope(db, RESOURCE, asset_id, access.scope)
