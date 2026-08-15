"""SQLAlchemy model for the automation send idempotency ledger.

One row per ``(automation_id, portal_mapping_id)`` pair that has already been
processed by ``automation_runner.py`` — its existence is the sole source of
truth for "have we already sent this automation for this workshop mapping",
replacing the old single-automation ``PortalMapping.pre_webinar_reminder_sent_on``
column now that multiple automations (of the same or different type) can
target the same mapping independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AutomationSendLedger(Base):
    __tablename__ = "automation_send_ledger"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("email_automation.id", ondelete="CASCADE"), nullable=False
    )
    portal_mapping_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("portal_mapping.id", ondelete="CASCADE"), nullable=False
    )
    sent_on: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("automation_id", "portal_mapping_id", name="uq_automation_send_ledger"),
        Index("idx_automation_send_ledger_automation_id", "automation_id"),
    )
