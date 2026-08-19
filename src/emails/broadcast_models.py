"""SQLAlchemy model for one-off, super-admin-authored broadcast emails.

A ``Broadcast`` is a draft that a super admin composes (subject + Tiptap body)
and targets at an audience via the filter resolved by
``audience.resolve_audience``. Sending fans out to ``EmailSendLog`` rows
(``source="broadcast"``, ``broadcast_id`` FK) via the shared render/send
pipeline — see ``broadcast_send.py``.

``body_json``, ``school_ids`` and ``cohort_ids`` are stored as JSON strings (not
JSONB columns) to match the existing ``CommunicationTemplate.content``
convention and stay portable across the Postgres prod DB and the in-memory
SQLite DB used in tests.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class Broadcast(Base):
    __tablename__ = "broadcast"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Tiptap JSON document, serialized as a string (see module docstring).
    body_json: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON arrays of school_id / cohort_id strings. Both empty = every customer
    # school; otherwise the union of the listed schools and the schools in the
    # listed cohorts (see resolve_audience).
    school_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    cohort_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    role_filter: Mapped[str] = mapped_column(Text, nullable=False, default="all", server_default="all")
    opt_in_filter: Mapped[str] = mapped_column(
        Text, nullable=False, default="opted_in", server_default="opted_in"
    )
    # From identity for this send. NULL falls back to settings.ses_from_email —
    # see emails/sender.py, which also validates the domain before a row is saved.
    sender_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True = one email per school addressed to all of that school's recipients
    # (multiple To: addresses, names joined in {{recipient_first_names}}).
    group_by_school: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")

    @property
    def school_id_list(self) -> list[str]:
        """``school_ids`` decoded; an unparseable value degrades to no restriction."""
        return _decode_id_list(self.school_ids)

    @property
    def cohort_id_list(self) -> list[str]:
        """``cohort_ids`` decoded; an unparseable value degrades to no restriction."""
        return _decode_id_list(self.cohort_ids)

    __table_args__ = (
        CheckConstraint("role_filter IN ('all', 'hub_admin')", name="ck_broadcast_role_filter"),
        CheckConstraint("opt_in_filter IN ('opted_in', 'all')", name="ck_broadcast_opt_in_filter"),
        CheckConstraint(
            "status IN ('draft', 'sending', 'sent', 'failed')", name="ck_broadcast_status"
        ),
    )


def _decode_id_list(raw: str | None) -> list[str]:
    """Decode a stored JSON id array. Anything malformed collapses to ``[]`` —
    the same degrade-to-empty stance ``resolve_audience`` takes for a bad id,
    rather than raising deep inside a background send."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]
