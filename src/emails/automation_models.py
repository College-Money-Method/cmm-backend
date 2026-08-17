"""SQLAlchemy model for scheduler-driven email automations (pre-workshop
reminder, post-workshop follow-up, ...).

Full admin CRUD lives in ``automation_router.py``; the scheduler read path
lives in ``automation_runner.py``. Both share this model.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class EmailAutomation(Base):
    """One row per admin-configured automation.

    ``type`` picks the scheduler behavior (which anchor/opt-in logic and which
    ``EmailSendLog.source`` its sends log under). ``offset_value`` +
    ``offset_unit`` + ``offset_direction`` together compute the fire time
    relative to ``Webinar.start_datetime`` — see ``automation_runner.py`` for
    the exact window math. The opt-in recipient filter
    (``Contact.auto_emails is True``) is hardcoded in the runner and has no
    override field on this table, by design (contrast with Broadcast's
    ``opt_in_filter``).
    """

    __tablename__ = "email_automation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # "pre_workshop_reminder" | "post_workshop_reminder" (extensible).
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Ship-dark default: the scheduler skips this automation entirely while False.
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    offset_value: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    offset_unit: Mapped[str] = mapped_column(Text, nullable=False, default="days", server_default="days")
    offset_direction: Mapped[str] = mapped_column(Text, nullable=False, default="before", server_default="before")
    # Required by the scheduler at send time (must be category="workshop");
    # nullable here only so a freshly-created automation can be saved before a
    # template is picked — the runner skips (and retries later) until it's set.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("email_template.id", ondelete="SET NULL"), nullable=True
    )
    # Optional override of the resolved template's subject line.
    subject_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True, onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "type IN ('pre_workshop_reminder', 'post_workshop_reminder')", name="ck_email_automation_type"
        ),
        CheckConstraint("offset_unit IN ('days', 'hours')", name="ck_email_automation_offset_unit"),
        CheckConstraint("offset_direction IN ('before', 'after')", name="ck_email_automation_offset_direction"),
    )
