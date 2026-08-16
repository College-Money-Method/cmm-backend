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

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.emails.automation_models import EmailAutomation
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog

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
    sent_count: int


class EmailAutomationCreate(BaseModel):
    name: str = Field(min_length=1)
    type: AutomationType
    offset_value: int = Field(gt=0)
    offset_unit: OffsetUnit
    offset_direction: OffsetDirection
    template_id: uuid.UUID | None = None
    subject_override: str | None = None
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
        sent_count=_sent_count(db, automation.id),
    )


def _validate_template_id(db: Session, template_id: uuid.UUID | None) -> None:
    """Reject a template_id that doesn't resolve to a workshop_automation
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
    if template.category != "workshop_automation":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="template_id must reference a workshop_automation template",
        )


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
    automation = EmailAutomation(**payload.model_dump())
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


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation(automation_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    automation = _get_automation_or_404(db, automation_id)
    db.delete(automation)
    db.commit()
