"""User role model — links Supabase Auth user IDs to app roles."""

import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


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
        SAEnum("super_admin", "hub_admin", "hub_user", "viewer", name="app_role_enum"),
        nullable=False,
        default="hub_user",
    )
    # Only set for hub roles — links them to their school
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Optional job title (e.g. "Principal", "Vice Principal"); defaults to "<School> Counselor"
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    # Airtable job role (Director/Counselor) — display only, drives no access logic
    school_role: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )

    school: Mapped["School"] = relationship("School", foreign_keys=[school_id])


class Profile(Base):
    """Person-centric mirror of Supabase ``auth.users`` (email + name).

    Kept in sync on every account create/update so counselor search and
    pagination run as one indexed SQL join instead of enumerating the whole
    Supabase Auth directory. Keyed by user_id (one row per person), so it stays
    correct even if a person later holds multiple roles.
    """

    __tablename__ = "profiles"

    # References auth.users(id) in Supabase — not a FK to avoid cross-schema FK issues
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )
