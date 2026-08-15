"""SQLAlchemy model for one-off, super-admin-authored broadcast emails.

A ``Broadcast`` is a draft that a super admin composes (subject + Tiptap body)
and targets at an audience via the 3-dimension filter resolved by
``audience.resolve_audience``. Sending fans out to ``EmailSendLog`` rows
(``source="broadcast"``, ``broadcast_id`` FK) via the shared render/send
pipeline — see ``broadcast_send.py``.

``body_json`` is stored as a JSON string (not a JSONB column) to match the
existing ``CommunicationTemplate.content`` convention and stay portable across
the Postgres prod DB and the in-memory SQLite DB used in tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Text, Uuid
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
    # "all_customers" or a specific school_id (str(uuid.UUID)).
    school_scope: Mapped[str] = mapped_column(Text, nullable=False)
    role_filter: Mapped[str] = mapped_column(Text, nullable=False, default="all", server_default="all")
    opt_in_filter: Mapped[str] = mapped_column(
        Text, nullable=False, default="opted_in", server_default="opted_in"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")

    __table_args__ = (
        CheckConstraint("role_filter IN ('all', 'hub_admin')", name="ck_broadcast_role_filter"),
        CheckConstraint("opt_in_filter IN ('opted_in', 'all')", name="ck_broadcast_opt_in_filter"),
        CheckConstraint(
            "status IN ('draft', 'sending', 'sent', 'failed')", name="ck_broadcast_status"
        ),
    )
