"""SQLAlchemy model for the global application config (singleton).

A single-row table holding site-wide settings that are not scoped to any
school — currently the global "welcome video" shown on school home pages.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AppConfig(Base):
    __tablename__ = "app_config"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Global welcome video — a Vimeo iframe embed code (same convention as
    # workshops/content video_embed_code). Null = no video configured.
    welcome_video_embed_code: Mapped[str | None] = mapped_column(Text)
    welcome_video_title: Mapped[str | None] = mapped_column(Text)
    welcome_video_caption: Mapped[str | None] = mapped_column(Text)

    # Global "topic overview" video shown on the school topics/journey page.
    # A plain video URL (e.g. Vimeo/YouTube link). Null = no video configured
    # (the "Watch video overview" button is hidden).
    topic_overview_video_url: Mapped[str | None] = mapped_column(Text)

    # IANA zone that workshop dates/times render in, for every school. Null =
    # fall back to the env seed (settings.workshop_display_timezone).
    # See src/schools/display_timezone.py.
    workshop_display_timezone: Mapped[str | None] = mapped_column(Text)

    # Vimeo folder that audit runs are uploaded into. Null = fall back to the
    # env seed (settings.vimeo_audit_folder_uri). Editable because which folder
    # an audit lands in is an operator's call, not a deploy's.
    # See src/app_config/operator_settings.py.
    vimeo_audit_folder_uri: Mapped[str | None] = mapped_column(Text)
    # Folders production replays and trailer reels are uploaded into. Same
    # rules: null falls back to the env seed, and an admin edits them.
    vimeo_replay_folder_uri: Mapped[str | None] = mapped_column(Text)
    vimeo_reel_folder_uri: Mapped[str | None] = mapped_column(Text)

    # Feature flags
    survey_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)

    # Email sandbox mode. When True, outbound email only reaches recipients on
    # the team domain (collegemoneymethod.com); every other recipient is logged
    # (status="sandboxed") but never sent — on ANY environment. Off = normal
    # production sending to real recipients. Default False; typically turned on
    # in local/dev and left off in production.
    email_sandbox_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # "Request a call with Paul" (see src/paul_booking). Off by default: it is the
    # kill switch, and nothing is offered until Paul has set hours below.
    paul_booking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # IANA zone Paul's weekly hours and days off are written in. Null = Pacific.
    paul_booking_timezone: Mapped[str | None] = mapped_column(Text)
    # {"mon": {"start": "09:00", "end": "17:00"}, "sat": null, ...}; one range
    # per weekday, a missing or null day takes no calls. Validated on write.
    paul_booking_weekly_hours: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    # Inbox for new requests and confirmations (also the .ics organizer).
    # Null = Paul's own address (rules.DEFAULT_NOTIFY_EMAIL).
    paul_booking_notify_email: Mapped[str | None] = mapped_column(Text)

    # College Aid Explorer (see src/colleges): one shared password. scrypt
    # "salt$hash", never plaintext. Null = the tool is closed to everyone.
    college_aid_password_hash: Mapped[str | None] = mapped_column(Text)
    college_aid_password_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Where the explorer's "Find your SAI" link points (an outside SAI/FAFSA
    # calculator). Null = no link is shown.
    college_aid_sai_calculator_url: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
