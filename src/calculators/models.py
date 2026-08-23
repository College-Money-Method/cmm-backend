"""SQLAlchemy models for embeddable calculators.

A calculator is an admin-authored HTML/CSS/JS document plus a JSON payload of
type-specific data. The backend is pure storage — no calculation happens here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# Both the data-entry form and the render path are keyed off ``type``, so adding
# a value here also needs a matching config descriptor on the frontend.
CALCULATOR_TYPES = (
    "fafsa_sai",
    "business_net_worth",
    "application_assets",
    "student_borrowing_8_percent",
)

CALCULATOR_STATUSES = ("draft", "published")


class Calculator(Base):
    __tablename__ = "calculators"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Full authored markup: HTML + <style> + <script>. Stored UNSANITIZED by
    # design. This is first-party super_admin content, and sanitizing here would
    # strip the <script> that *is* the calculator. Isolation is the frontend's
    # job: it serves this as its own top-level document under /embed and frames
    # that everywhere, so the blob never shares a document with the site.
    html: Mapped[str | None] = mapped_column(Text)

    # Shared <link>/<style>/<script src> tags injected into the render <head>,
    # mirroring the Tiptap rawHtml node's `deps` attribute.
    deps: Mapped[str | None] = mapped_column(Text)

    # Type-specific data: yearly constants, lookup tables, thresholds. Injected
    # into the render as window.__CALC_CONFIG__ so that a yearly policy update
    # is a form edit rather than a markup edit.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    # Origins permitted to frame this calculator, as a JSON array of strings.
    # An empty array means any origin. JSONB rather than ARRAY(Text) because the
    # test suite runs on SQLite, which has no array type but does have a JSONB
    # compile shim (see tests/emails/conftest.py).
    embed_allowed_origins: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    meta_title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_calculators_slug", "slug"),
        Index("idx_calculators_status", "status"),
        Index("idx_calculators_type", "type"),
    )
