"""SQLAlchemy models for the content management system."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class AssetType(Base):
    __tablename__ = "asset_types"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    content_assets: Mapped[list[ContentAsset]] = relationship(back_populates="asset_type")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    suggested_grades: Mapped[str | None] = mapped_column(Text)  # e.g. "9,10"
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    content_assets: Mapped[list[ContentAsset]] = relationship(
        secondary="content_asset_topics",
        back_populates="topics",
        order_by="ContentAssetTopic.sort_order",
        viewonly=True,
    )


class Objective(Base):
    __tablename__ = "objectives"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    workshops = relationship("Workshop", secondary="objective_workshops", back_populates="objectives")
    content_assets: Mapped[list[ContentAsset]] = relationship(
        secondary="content_asset_objectives", back_populates="objectives"
    )


class ContentAsset(Base):
    __tablename__ = "content_assets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    asset_type_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("asset_types.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)   # rich HTML for "What You'll Learn" card
    content: Mapped[str | None] = mapped_column(Text)   # sanitized HTML, edited via Tiptap
    action_items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    link: Mapped[str | None] = mapped_column(Text)
    embed_code: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    file_url: Mapped[str | None] = mapped_column(Text)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    wp_post_id: Mapped[str | None] = mapped_column(Text)
    wp_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    asset_type: Mapped[AssetType | None] = relationship(back_populates="content_assets")
    objectives: Mapped[list[Objective]] = relationship(
        secondary="content_asset_objectives", back_populates="content_assets"
    )
    topics: Mapped[list[Topic]] = relationship(
        secondary="content_asset_topics", back_populates="content_assets"
    )
    workshops = relationship("Workshop", secondary="content_asset_workshops", back_populates="content_assets")
    cohorts = relationship("Cohort", secondary="content_asset_cohorts", back_populates="content_assets")
    faqs: Mapped[list[Faq]] = relationship(
        secondary="content_asset_faqs",
        order_by="ContentAssetFaq.sort_order",
        viewonly=True,
    )
    resources: Mapped[list[ContentAsset]] = relationship(
        "ContentAsset",
        secondary="content_asset_resources",
        primaryjoin="ContentAsset.id == ContentAssetResource.content_asset_id",
        secondaryjoin="ContentAsset.id == ContentAssetResource.resource_id",
        order_by="ContentAssetResource.sort_order",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_content_assets_asset_type_id", "asset_type_id"),
        Index("idx_content_assets_status", "status"),
        Index("idx_content_assets_airtable_id", "airtable_id"),
    )


# ── Join tables ───────────────────────────────────────────────────────────────

class ObjectiveWorkshop(Base):
    __tablename__ = "objective_workshops"

    objective_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("objectives.id", ondelete="CASCADE"), primary_key=True
    )
    workshop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workshops.id", ondelete="CASCADE"), primary_key=True
    )


class ContentAssetObjective(Base):
    __tablename__ = "content_asset_objectives"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    objective_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("objectives.id", ondelete="CASCADE"), primary_key=True
    )


class ContentAssetWorkshop(Base):
    __tablename__ = "content_asset_workshops"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    workshop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workshops.id", ondelete="CASCADE"), primary_key=True
    )


class ContentAssetTopic(Base):
    __tablename__ = "content_asset_topics"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ContentAssetCohort(Base):
    __tablename__ = "content_asset_cohorts"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cohorts.id", ondelete="CASCADE"), primary_key=True
    )


# ── Faqs ──────────────────────────────────────────────────────────────────────

class Faq(Base):
    __tablename__ = "faqs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class ContentAssetFaq(Base):
    __tablename__ = "content_asset_faqs"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("faqs.id", ondelete="CASCADE"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ContentAssetResource(Base):
    __tablename__ = "content_asset_resources"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ReaderQuestion(Base):
    __tablename__ = "reader_questions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
