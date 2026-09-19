"""Webinar Q&A: ingested questions, derived answer extractions, recording start

Zoom's Q&A report is pulled per webinar into ``webinar_qa_questions``, keyed on
Zoom's own question id so a retry cannot duplicate a row. Questions a panelist
answered out loud carry no answer text in Zoom at all; those answers are
recovered from the video pipeline's transcript into
``webinar_qa_answer_extractions``, which is append-only so a re-run under a
better prompt adds a verdict instead of overwriting one.

``webinar_video_jobs.recording_start`` is the wall-clock origin of the
transcript clock. Without it a spoken answer cannot be placed in real time, so
it cannot be checked against when the question was asked.

Revision ID: 0125
Revises: 0124
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webinar_video_jobs",
        sa.Column("recording_start", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "webinar_qa_syncs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "webinar_id",
            sa.Uuid(),
            sa.ForeignKey("webinars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("zoom_webinar_id", sa.Text(), nullable=False),
        sa.Column("zoom_webinar_uuid", sa.Text(), nullable=True),
        sa.Column("webinar_start_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ok"),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "synced_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_webinar_qa_syncs_webinar_id", "webinar_qa_syncs", ["webinar_id"])
    op.create_index("idx_webinar_qa_syncs_synced_at", "webinar_qa_syncs", ["synced_at"])

    op.create_table(
        "webinar_qa_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "sync_id",
            sa.Uuid(),
            sa.ForeignKey("webinar_qa_syncs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "webinar_id",
            sa.Uuid(),
            sa.ForeignKey("webinars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The idempotency key. UNIQUE, not indexed — a re-sync relies on the
        # constraint to collapse a question it has already seen.
        sa.Column("zoom_question_id", sa.Text(), nullable=False, unique=True),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("asked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("question_status", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("asker_name", sa.Text(), nullable=True),
        sa.Column("asker_email", sa.Text(), nullable=True),
        sa.Column("asker_zoom_user_id", sa.Text(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "registration_id",
            sa.Uuid(),
            sa.ForeignKey("workshop_registrations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("answer_source", sa.Text(), nullable=False, server_default="unanswered"),
        sa.Column("typed_answer_text", sa.Text(), nullable=True),
        sa.Column("answer_visibility", sa.Text(), nullable=True),
        sa.Column("marked_answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("responder_name", sa.Text(), nullable=True),
        sa.Column("responder_email", sa.Text(), nullable=True),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("classified_by", sa.Text(), nullable=True),
        sa.Column("classified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("answer_text_override", sa.Text(), nullable=True),
        sa.Column("classification_override", sa.Text(), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("edited_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("edited_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_webinar_qa_questions_webinar_id", "webinar_qa_questions", ["webinar_id"]
    )
    op.create_index(
        "idx_webinar_qa_questions_classification", "webinar_qa_questions", ["classification"]
    )
    op.create_index("idx_webinar_qa_questions_asked_at", "webinar_qa_questions", ["asked_at"])

    op.create_table(
        "webinar_qa_answer_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "question_id",
            sa.Uuid(),
            sa.ForeignKey("webinar_qa_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "video_job_id",
            sa.Uuid(),
            sa.ForeignKey("webinar_video_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("transcript_start_seconds", sa.Integer(), nullable=True),
        sa.Column("transcript_end_seconds", sa.Integer(), nullable=True),
        sa.Column("answered_by", sa.Text(), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("transcript_excerpt", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_webinar_qa_extractions_question_id", "webinar_qa_answer_extractions", ["question_id"]
    )
    op.create_index(
        "idx_webinar_qa_extractions_video_job_id",
        "webinar_qa_answer_extractions",
        ["video_job_id"],
    )


def downgrade() -> None:
    op.drop_table("webinar_qa_answer_extractions")
    op.drop_table("webinar_qa_questions")
    op.drop_table("webinar_qa_syncs")
    op.drop_column("webinar_video_jobs", "recording_start")
