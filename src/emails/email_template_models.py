"""SQLAlchemy model for CMM-branded, reusable email templates.

Distinct from the counselor-facing ``CommunicationTemplate`` /
``WorkshopEmailTemplate`` models — those are authored per-school/per-workshop
by hub admins; ``EmailTemplate`` rows are managed centrally in the Emails hub
by super admins and are one-time **prefill** sources (copy subject + body),
never a live link — a Broadcast or EmailAutomation that "used" a template at
creation time keeps its own independent copy of the content afterward.

``category`` scopes which picker a template shows up in: ``"broadcast"``
templates prefill the Broadcast composer; ``"workshop_automation"`` templates
are the (required) content source for automation sends (see
``automation_runner.py``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class EmailTemplate(Base):
    __tablename__ = "email_template"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Tiptap JSON document, serialized as a string (matches Broadcast.body_json
    # so the shared `render_email` pipeline accepts either without branching).
    body_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True, onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "category IN ('broadcast', 'workshop_automation')", name="ck_email_template_category"
        ),
        Index("idx_email_template_category", "category"),
    )
