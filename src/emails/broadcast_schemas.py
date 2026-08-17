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
    # "all_customers" or a school_id (uuid) string — validated against real
    # schools at send/preview time, not here (a stale/forged id just matches
    # nothing once resolve_audience's customer-school restriction applies).
    school_scope: str = Field(min_length=1)
    role_filter: RoleFilter = "all"
    opt_in_filter: OptInFilter = "opted_in"


class BroadcastOut(BaseModel):
    id: uuid.UUID
    subject: str
    body_json: dict
    school_scope: str
    role_filter: str
    opt_in_filter: str
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
