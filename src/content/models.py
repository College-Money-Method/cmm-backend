"""SQLAlchemy models for the content management system."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class AssetType(Base):
    __tablename__ = "asset_types"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    is_upload: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    content_assets: Mapped[list[ContentAsset]] = relationship(back_populates="asset_type")


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    airtable_id: Mapped[str | None] = mapped_column(Text, unique=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("goals.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    icon_url: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    suggested_grades: Mapped[str | None] = mapped_column(Text)  # e.g. "9,10"
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


    content_assets: Mapped[list[ContentAsset]] = relationship(
        secondary="content_asset_goals",
        back_populates="goals",
        order_by="ContentAssetGoal.sort_order",
        viewonly=True,
    )

    topics: Mapped[list[Topic]] = relationship(
        back_populates="goal",
        order_by="Topic.sort_order",
    )



class Topic(Base):
    """Content-rich topic page — like a ContentAsset but with its own entity."""
    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)  # rich HTML for "What You'll Learn"
    content: Mapped[str | None] = mapped_column(Text)  # sanitized HTML, edited via Tiptap
    action_items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    video_embed_code: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    goal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("goals.id", ondelete="SET NULL"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    goal: Mapped[Goal | None] = relationship(back_populates="topics")

    faqs: Mapped[list[Faq]] = relationship(
        secondary="topic_faqs",
        order_by="TopicFaq.sort_order",
        viewonly=True,
    )
    resources: Mapped[list[ContentAsset]] = relationship(
        secondary="topic_resources",
        order_by="TopicResource.sort_order",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_topics_slug", "slug"),
        Index("idx_topics_status", "status"),
        Index("idx_topics_goal_id", "goal_id"),
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
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    read_time_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    asset_type: Mapped[AssetType | None] = relationship(back_populates="content_assets")
    objectives: Mapped[list[Objective]] = relationship(
        secondary="content_asset_objectives", back_populates="content_assets"
    )
    goals: Mapped[list[Goal]] = relationship(
        secondary="content_asset_goals", back_populates="content_assets"
    )
    topics: Mapped[list[Topic]] = relationship(
        secondary="topic_resources",
        primaryjoin="ContentAsset.id == foreign(TopicResource.content_asset_id)",
        secondaryjoin="Topic.id == foreign(TopicResource.topic_id)",
        order_by="TopicResource.sort_order",
        viewonly=True,
    )
    workshops = relationship("Workshop", secondary="workshop_resources", back_populates="content_assets")
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


class WorkshopResource(Base):
    __tablename__ = "workshop_resources"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    workshop_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workshops.id", ondelete="CASCADE"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ContentAssetGoal(Base):
    __tablename__ = "content_asset_goals"

    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("goals.id", ondelete="CASCADE"), primary_key=True
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


class TopicFaq(Base):
    __tablename__ = "topic_faqs"

    topic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("faqs.id", ondelete="CASCADE"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class TopicResource(Base):
    __tablename__ = "topic_resources"

    topic_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    content_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("content_assets.id", ondelete="CASCADE"), primary_key=True
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


# ── Grade Sets & Grade Configs ──────────────────────────────────────────────


class GradeSet(Base):
    """A named collection of grade configurations (e.g. 'Standard 9th–12th')."""
    __tablename__ = "grade_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    grade_configs: Mapped[list[GradeConfig]] = relationship(back_populates="grade_set")


class GradeConfig(Base):
    """Per-grade configuration within a grade set."""
    __tablename__ = "grade_configs"
    __table_args__ = (
        UniqueConstraint("grade_set_id", "grade", name="uq_grade_configs_grade_set_grade"),
        Index("idx_grade_configs_grade_set_id", "grade_set_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    grade_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("grade_sets.id", ondelete="RESTRICT"), nullable=False
    )
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "9th Grade"
    description: Mapped[str | None] = mapped_column(Text)
    video_overview_url: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(Text)  # lucide icon name e.g. "BookOpen"
    bg_color: Mapped[str | None] = mapped_column(Text)  # e.g. "rgba(255, 242, 246, 0.6)"
    page_title: Mapped[str | None] = mapped_column(Text)
    page_description: Mapped[str | None] = mapped_column(Text)
    banner_image_url: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    grade_set: Mapped[GradeSet] = relationship(back_populates="grade_configs")
    goals: Mapped[list[Goal]] = relationship(
        secondary="grade_config_goals",
        order_by="GradeConfigGoal.sort_order",
        viewonly=True,
    )


class GradeConfigGoal(Base):
    """Join table linking a grade config to selected goals."""
    __tablename__ = "grade_config_goals"

    grade_config_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("grade_configs.id", ondelete="CASCADE"), primary_key=True
    )
    goal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("goals.id", ondelete="CASCADE"), primary_key=True
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
