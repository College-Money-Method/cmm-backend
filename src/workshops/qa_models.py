"""SQLAlchemy models for webinar Q&A — what attendees asked and how it was answered.

Three tables, deliberately separate, because they have three different lifecycles:

1. ``webinar_qa_syncs`` / ``webinar_qa_questions`` — **ingested fact** from Zoom's
   Q&A report. Re-synced freely; never rewritten by our own inference.
2. ``webinar_qa_answer_extractions`` — **derived inference**. When a question was
   "live answered" Zoom stores no answer text at all, so the answer is recovered
   from the video pipeline's transcript by an LLM. Append-only and versioned by
   prompt, so a better prompt adds a row instead of destroying the old verdict.
3. The ``*_override`` columns on a question — **an admin's edit**. These win over
   both of the above and must survive a re-sync and a re-extraction.

Collapsing those layers means a re-run silently destroys an admin's correction,
which is why the separation is in the schema rather than in service-layer manners.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base

if TYPE_CHECKING:
    from src.video_pipeline.models import WebinarVideoJob
    from src.workshops.models import Webinar, WorkshopRegistration


# Vocabularies are plain Text rather than a Postgres ENUM: every one of these is
# expected to gain members (a new noise label, a new extraction outcome), and a
# warehouse consumer reading the table should not need our enum definitions. The
# tuples below are the contract; they are what the services validate against.
SYNC_STATUSES = ("ok", "partial", "failed")
ANSWER_SOURCES = ("typed", "live", "unanswered")
ANSWER_VISIBILITIES = ("public", "private")
CLASSIFICATIONS = ("question", "greeting", "thanks", "comment", "spam")
CLASSIFIED_BY = ("rule", "llm", "admin")
EXTRACTION_STATUSES = ("extracted", "not_found", "presentation_coverage", "failed")


class WebinarQaSync(Base):
    """One run of the Zoom Q&A report pull, with the payload it saw.

    ``raw_payload`` is kept so the questions can be reprocessed — a better noise
    classifier, a new field we did not parse — without calling Zoom again, and so
    the warehouse has lineage back to the exact bytes that produced a row.
    """

    __tablename__ = "webinar_qa_syncs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    webinar_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False
    )
    zoom_webinar_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Zoom's per-occurrence UUID. A recurring webinar reuses `zoom_webinar_id`
    # across every session, so this is the only field that says which one.
    zoom_webinar_uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    webinar_start_time: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok", server_default="ok")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    webinar: Mapped[Webinar] = relationship("Webinar")

    __table_args__ = (
        Index("idx_webinar_qa_syncs_webinar_id", "webinar_id"),
        Index("idx_webinar_qa_syncs_synced_at", "synced_at"),
    )


class WebinarQaQuestion(Base):
    """One submission to the Q&A panel — the grain of the whole feature.

    "Submission" rather than "question" on purpose: a third of what arrives is a
    greeting, a joke, or a thank-you. Those are classified and hidden, never
    deleted, because the raw stream is itself a signal about the audience.
    """

    __tablename__ = "webinar_qa_questions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The sync that most recently wrote this row. Provenance, not ownership —
    # a re-sync repoints it, and the row survives the older sync being pruned.
    sync_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("webinar_qa_syncs.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalised from the sync so warehouse queries and the admin list can
    # filter by webinar without a join through the provenance table.
    webinar_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False
    )
    # Idempotency key: Zoom's own question id, stable across report pulls. The
    # UNIQUE constraint is what makes re-syncing safe — never relax it to a
    # plain index, or the retry ladder duplicates every question it re-reads.
    zoom_question_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    asked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # Zoom's own word for the state of the question ("open", "answered", ...).
    question_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SHA-256 of the question text. Lets a re-sync notice Zoom changed the body
    # (it does not, today) without diffing long strings, and gives the warehouse
    # a cheap change-data-capture handle.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    asker_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    asker_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    asker_zoom_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Nullable and expected to be null often: roughly half of a real webinar's
    # askers are anonymous, and an anonymous submission carries no email to
    # match a registration on. A NOT NULL here would reject half the data.
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workshop_registrations.id", ondelete="SET NULL"), nullable=True
    )

    answer_source: Mapped[str] = mapped_column(
        Text, nullable=False, default="unanswered", server_default="unanswered"
    )
    # Present only for `typed`. Zoom stores nothing at all for a question a
    # panelist answered out loud — that is what the extraction table is for.
    typed_answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_visibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    marked_answered_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    responder_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    responder_email: Mapped[str | None] = mapped_column(Text, nullable=True)

    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    classified_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # --- admin override: wins over everything above, survives every re-run ---
    answer_text_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_hidden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Supabase ``auth.users(id)``. Not a FK — the rest of the codebase avoids
    # cross-schema foreign keys into the auth schema (see ``UserRole.user_id``).
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    webinar: Mapped[Webinar] = relationship("Webinar")
    registration: Mapped[WorkshopRegistration | None] = relationship("WorkshopRegistration")
    extractions: Mapped[list[WebinarQaAnswerExtraction]] = relationship(
        back_populates="question",
        cascade="all, delete",
        passive_deletes=True,
        order_by="WebinarQaAnswerExtraction.created_at",
    )

    __table_args__ = (
        Index("idx_webinar_qa_questions_webinar_id", "webinar_id"),
        Index("idx_webinar_qa_questions_classification", "classification"),
        Index("idx_webinar_qa_questions_asked_at", "asked_at"),
    )


class WebinarQaAnswerExtraction(Base):
    """One attempt at recovering a spoken answer from the transcript.

    Append-only. A re-run under a new prompt inserts a row; nothing is updated in
    place. That is deliberate — extraction is non-deterministic on borderline
    questions (the same question has returned both ``presentation_coverage`` and
    ``not_found`` on different runs), so keeping the history is the only way to
    tell a prompt regression from ordinary model variance.

    ``status`` distinguishes three different kinds of "no answer":
    ``not_found`` (nothing in the transcript matches), ``presentation_coverage``
    (the topic is covered, but *before* the question was asked — the attendee
    asked because they heard it, so it is not an answer to them), and ``failed``
    (the run itself broke).
    """

    __tablename__ = "webinar_qa_answer_extractions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("webinar_qa_questions.id", ondelete="CASCADE"), nullable=False
    )
    # Which transcript this verdict came from. Nullable on purpose: the job row
    # can be pruned, and losing it should not cost us the answer text.
    video_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("webinar_video_jobs.id", ondelete="SET NULL"), nullable=True
    )
    # Seconds on the TRIMMED transcript clock — the same clock the published
    # replay uses, so these double as a deep link into the video. Absolute wall
    # time is `recording_start + trim_offset_seconds + transcript_start_seconds`.
    transcript_start_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_end_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answered_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The verbatim cues behind `answer_text`, so an admin can check the summary
    # against what was actually said without opening the video.
    transcript_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bumped whenever the prompt changes. Without it, comparing two runs tells
    # you nothing about whether the prompt or the model moved.
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    question: Mapped[WebinarQaQuestion] = relationship(back_populates="extractions")
    video_job: Mapped[WebinarVideoJob | None] = relationship("WebinarVideoJob")

    __table_args__ = (
        Index("idx_webinar_qa_extractions_question_id", "question_id"),
        Index("idx_webinar_qa_extractions_video_job_id", "video_job_id"),
    )
