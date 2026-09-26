"""SQLAlchemy model for guest contact form submissions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from src.db.base import Base


class GuestContact(Base):
    __tablename__ = "guest_contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    first_name: Mapped[str] = mapped_column(Text, nullable=False)
    last_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    school_name: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    # Bot submissions are quarantined rather than rejected — see
    # ``spam_detection``. ``spam_reason`` names the rule that matched.
    # text() rather than the string "false": as a plain string SQLAlchemy renders a
    # quoted literal, which SQLite stores as the text 'false' and reads back truthy.
    is_spam: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    spam_reason: Mapped[str | None] = mapped_column(Text)

    # Parents writing about their own child are welcome but off-target for a form
    # meant for schools, so they get their own tab. Independent of is_spam: a
    # parent enquiry is real mail, it is only filed elsewhere.
    is_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    parent_reason: Mapped[str | None] = mapped_column(Text)

    # When an admin replied. Null means the enquiry is still waiting on us, which
    # is the only state the inbox really needs to distinguish; keeping the moment
    # rather than a bare flag also answers "how long did that one sit there".
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("idx_guest_contacts_email", "email"),
        Index("idx_guest_contacts_created_at", "created_at"),
        Index(
            "idx_guest_contacts_inbox",
            "created_at",
            postgresql_where=text("is_spam = false"),
        ),
    )
