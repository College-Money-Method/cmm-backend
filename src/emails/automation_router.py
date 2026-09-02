"""Admin CRUD endpoints over the `email_automation` table — super_admin ONLY
(same authz level as `broadcast_router`).

This router does NOT define the model or the scheduler read path (see
`automation_models.py` and `automation_runner.py`) — it only exposes a
management surface for the Automations admin tab: create/list/patch/delete
rows, each with a live `sent_count` derived from `EmailSendLog`.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.emails.automation_models import EmailAutomation
from src.emails.automation_send_log_queries import AutomationSendPage, automation_sends
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.emails.sender import InvalidSenderError, validate_sender

router = APIRouter(prefix="/api/v1/emails/automations", tags=["emails"])

AutomationType = Literal["pre_workshop_reminder", "post_workshop_reminder"]
OffsetUnit = Literal["days", "hours"]
OffsetDirection = Literal["before", "after"]

# A pre-workshop reminder fires *before* the workshop, a post-workshop reminder
# *after* — the direction is fully determined by the type. Enforced here so the
# rule holds regardless of client (the admin UI also derives it from type).
_REQUIRED_DIRECTION: dict[str, OffsetDirection] = {
    "pre_workshop_reminder": "before",
    "post_workshop_reminder": "after",
}


class EmailAutomationOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    enabled: bool
    offset_value: int
    offset_unit: str
    offset_direction: str
    template_id: uuid.UUID | None
    subject_override: str | None
    sender_name: str | None = None
    sender_email: str | None = None
    sent_count: int


class EmailAutomationCreate(BaseModel):
    name: str = Field(min_length=1)
    type: AutomationType
    offset_value: int = Field(gt=0)
    offset_unit: OffsetUnit
    offset_direction: OffsetDirection
    template_id: uuid.UUID | None = None
    subject_override: str | None = None
    # From identity. Blank = the configured default; the domain is validated
    # against the sending allowlist in the router (see emails/sender.py).
    sender_name: str | None = None
    sender_email: str | None = None
    enabled: bool = False

    @model_validator(mode="after")
    def _direction_matches_type(self) -> "EmailAutomationCreate":
        required = _REQUIRED_DIRECTION[self.type]
        if self.offset_direction != required:
            raise ValueError(f"{self.type} requires offset_direction '{required}'")
        return self


class EmailAutomationUpdate(BaseModel):
    name: str | None = None
    type: AutomationType | None = None
    enabled: bool | None = None
    offset_value: int | None = None
    offset_unit: OffsetUnit | None = None
    offset_direction: OffsetDirection | None = None
    template_id: uuid.UUID | None = None
    subject_override: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None

    @field_validator("offset_value")
    @classmethod
    def _offset_value_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("offset_value must be a positive integer")
        return value


def _sent_count(db: Session, automation_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(EmailSendLog)
            .where(EmailSendLog.automation_id == automation_id, EmailSendLog.status == "sent")
        )
        or 0
    )


def _automation_out(db: Session, automation: EmailAutomation) -> EmailAutomationOut:
    return EmailAutomationOut(
        id=automation.id,
        name=automation.name,
        type=automation.type,
        enabled=automation.enabled,
        offset_value=automation.offset_value,
        offset_unit=automation.offset_unit,
        offset_direction=automation.offset_direction,
        template_id=automation.template_id,
        subject_override=automation.subject_override,
        sender_name=automation.sender_name,
        sender_email=automation.sender_email,
        sent_count=_sent_count(db, automation.id),
    )


def _validate_template_id(db: Session, template_id: uuid.UUID | None) -> None:
    """Reject a template_id that doesn't resolve to a workshop
    template. Scheduler requires this category; a dangling FK would silently
    skip every send at runtime, so fail fast at the write boundary."""
    if template_id is None:
        return
    template = db.get(EmailTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="template_id does not reference an existing email template",
        )
    if template.category != "workshop":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="template_id must reference a workshop template",
        )


def _validated_sender(name: str | None, email: str | None) -> tuple[str | None, str | None]:
    """Normalize the admin-chosen From, rejecting an address the app may not send
    as. SES would reject an unverified identity per recipient at send time, which
    is far harder to act on than a 400 here."""
    try:
        return validate_sender(name, email)
    except InvalidSenderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _get_automation_or_404(db: Session, automation_id: uuid.UUID) -> EmailAutomation:
    automation = db.get(EmailAutomation, automation_id)
    if automation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Automation not found")
    return automation


@router.get("", response_model=list[EmailAutomationOut])
def list_automations(_admin: AdminDep, db: DbDep) -> list[EmailAutomationOut]:
    automations = db.scalars(select(EmailAutomation).order_by(EmailAutomation.name)).all()
    return [_automation_out(db, a) for a in automations]


@router.post("", response_model=EmailAutomationOut, status_code=status.HTTP_201_CREATED)
def create_automation(payload: EmailAutomationCreate, _admin: AdminDep, db: DbDep) -> EmailAutomationOut:
    _validate_template_id(db, payload.template_id)
    fields = payload.model_dump()
    fields["sender_name"], fields["sender_email"] = _validated_sender(
        payload.sender_name, payload.sender_email
    )
    automation = EmailAutomation(**fields)
    db.add(automation)
    db.commit()
    db.refresh(automation)
    return _automation_out(db, automation)


@router.patch("/{automation_id}", response_model=EmailAutomationOut)
def update_automation(
    automation_id: uuid.UUID,
    payload: EmailAutomationUpdate,
    _admin: AdminDep,
    db: DbDep,
) -> EmailAutomationOut:
    automation = _get_automation_or_404(db, automation_id)
    updates = payload.model_dump(exclude_unset=True)
    if "template_id" in updates:
        _validate_template_id(db, updates["template_id"])
    if "sender_name" in updates or "sender_email" in updates:
        # Validate the merged state so patching only one half of the pair still
        # normalizes both consistently.
        updates["sender_name"], updates["sender_email"] = _validated_sender(
            updates.get("sender_name", automation.sender_name),
            updates.get("sender_email", automation.sender_email),
        )
    for field, value in updates.items():
        setattr(automation, field, value)
    # Validate the direction/type invariant against the merged state so a patch
    # touching only one of the two fields can't leave the row inconsistent.
    required = _REQUIRED_DIRECTION[automation.type]
    if automation.offset_direction != required:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{automation.type} requires offset_direction '{required}'",
        )
    db.add(automation)
    db.commit()
    db.refresh(automation)
    return _automation_out(db, automation)


@router.get("/{automation_id}/sends", response_model=AutomationSendPage)
def list_automation_sends(
    automation_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    cycle_id: uuid.UUID | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> AutomationSendPage:
    """One page of this automation's individual sends, newest first — the
    per-recipient detail behind the row's `sent_count`. `cycle_id` scopes to
    sends for that cycle's webinars; omit it for every send ever."""
    _get_automation_or_404(db, automation_id)
    return automation_sends(db, automation_id, cycle_id=cycle_id, offset=offset, limit=limit)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation(automation_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    automation = _get_automation_or_404(db, automation_id)
    db.delete(automation)
    db.commit()
