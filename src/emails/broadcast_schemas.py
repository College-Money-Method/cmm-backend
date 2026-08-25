"""Pydantic schemas for the broadcast (one-off admin email) endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

RoleFilter = Literal["all", "hub_admin"]
OptInFilter = Literal["opted_in", "all"]


class BroadcastCreate(BaseModel):
    subject: str = Field(min_length=1)
    body_json: dict
    # Targeted school / cohort ids. Both empty = every customer school; otherwise
    # the union of the two. Ids are validated against real schools at
    # send/preview time, not here (a stale/forged id just matches nothing once
    # resolve_audience's customer-school restriction applies).
    school_ids: list[str] = Field(default_factory=list)
    cohort_ids: list[str] = Field(default_factory=list)
    role_filter: RoleFilter = "all"
    opt_in_filter: OptInFilter = "opted_in"
    # From identity. Blank = the configured default; the domain is validated
    # against the sending allowlist in the router (see emails/sender.py).
    sender_name: str | None = None
    sender_email: str | None = None
    # True = one email per school addressed to all of that school's recipients.
    group_by_school: bool = False
    # Carried over from the template that prefilled this broadcast; True renders
    # the CMM shell (logo + branded footer) instead of the plain message.
    include_branding: bool = False


class BroadcastOut(BaseModel):
    id: uuid.UUID
    subject: str
    body_json: dict
    school_ids: list[str]
    cohort_ids: list[str]
    role_filter: str
    opt_in_filter: str
    sender_name: str | None = None
    sender_email: str | None = None
    group_by_school: bool = False
    include_branding: bool = False
    created_by: uuid.UUID
    created_at: datetime
    status: str


class RecipientStatusRow(BaseModel):
    recipient_email: str
    status: str
    sent_at: datetime


class BroadcastDetailOut(BroadcastOut):
    sent_count: int = 0
    dry_run_count: int = 0
    sandboxed_count: int = 0
    suppressed_count: int = 0
    failed_count: int = 0
    recipients: list[RecipientStatusRow] = Field(default_factory=list)


class AudiencePreviewOut(BaseModel):
    matched_count: int
    non_opted_in_count: int
    warning: bool


class AudienceContactRow(BaseModel):
    """One resolved recipient shown in the editable recipient-list preview."""

    id: uuid.UUID
    full_name: str
    email: str
    school_name: str | None = None
    opted_in: bool


class SenderOptionOut(BaseModel):
    """One preset offered by the compose UI's From picker. Presets are
    suggestions — any address on an allowed domain may be typed instead."""

    name: str
    email: str


class SenderOptionsOut(BaseModel):
    presets: list[SenderOptionOut]
    allowed_domains: list[str]


class SendBroadcastRequest(BaseModel):
    """Optional body for the send endpoint. When ``recipient_contact_ids`` is
    provided, exactly those contacts are sent to (still customer-scoped and
    unsubscribe-suppressed); when omitted, the audience is re-resolved from the
    broadcast's stored filters (backward compatible)."""

    recipient_contact_ids: list[uuid.UUID] | None = None


class SendTestResultOut(BaseModel):
    sent_to: str
    used_sample_contact: bool


class EmailEngagementOut(BaseModel):
    """Open/click aggregates. Open counts are an UPPER BOUND — Apple Mail
    Privacy Protection pre-fetches tracking pixels, inflating opens. The UI
    surfaces this caveat; treat ``open_rate`` accordingly."""

    sent_count: int
    unique_opened: int
    unique_clicked: int
    open_rate: float
    click_rate: float
