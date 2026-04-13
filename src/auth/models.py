"""User role model — links Supabase Auth user IDs to app roles."""

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base
from src.db.enums import AppRole


class UserRole(Base):
    __tablename__ = "user_roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # References auth.users(id) in Supabase — not a FK to avoid cross-schema FK issues
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True
    )
    role: Mapped[str] = mapped_column(
        SAEnum("super_admin", "counselor", "viewer", name="app_role_enum"),
        nullable=False,
        default="counselor",
    )
    # Only set for counselor role — links them to their school
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Optional job title (e.g. "Principal", "Vice Principal"); defaults to "<School> Counselor"
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    school: Mapped["School"] = relationship("School", foreign_keys=[school_id])
