"""Admin CRUD endpoints over the `email_template` table — super_admin ONLY
(same authz level as `broadcast_router`).

CMM-branded, reusable templates managed in the Emails hub. `category` scopes
which picker a template appears in (`"general"` one-off-send prefill vs
`"workshop"` automation content source) and is immutable once set
— editing a template never retroactively changes which picker it belongs to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.emails.email_template_models import EmailTemplate

router = APIRouter(prefix="/api/v1/emails/templates", tags=["emails"])

TemplateCategory = Literal["general", "workshop"]


class EmailTemplateCreate(BaseModel):
    category: TemplateCategory
    name: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body_json: dict


class EmailTemplateUpdate(BaseModel):
    """`category` is intentionally absent — immutable after creation."""

    name: str | None = Field(default=None, min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    body_json: dict | None = None


class EmailTemplateOut(BaseModel):
    id: uuid.UUID
    category: str
    name: str
    subject: str
    body_json: dict
    created_at: datetime
    updated_at: datetime | None


def _template_out(template: EmailTemplate) -> EmailTemplateOut:
    return EmailTemplateOut(
        id=template.id,
        category=template.category,
        name=template.name,
        subject=template.subject,
        body_json=json.loads(template.body_json),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _get_template_or_404(db: Session, template_id: uuid.UUID) -> EmailTemplate:
    template = db.get(EmailTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return template


@router.get("", response_model=list[EmailTemplateOut])
def list_templates(
    _admin: AdminDep, db: DbDep, category: TemplateCategory | None = Query(None)
) -> list[EmailTemplateOut]:
    stmt = select(EmailTemplate).order_by(EmailTemplate.name)
    if category is not None:
        stmt = stmt.where(EmailTemplate.category == category)
    templates = db.scalars(stmt).all()
    return [_template_out(t) for t in templates]


@router.post("", response_model=EmailTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(payload: EmailTemplateCreate, admin: AdminDep, db: DbDep) -> EmailTemplateOut:
    template = EmailTemplate(
        category=payload.category,
        name=payload.name,
        subject=payload.subject,
        body_json=json.dumps(payload.body_json),
        created_by=admin.user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _template_out(template)


@router.get("/{template_id}", response_model=EmailTemplateOut)
def get_template(template_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> EmailTemplateOut:
    return _template_out(_get_template_or_404(db, template_id))


@router.patch("/{template_id}", response_model=EmailTemplateOut)
def update_template(
    template_id: uuid.UUID, payload: EmailTemplateUpdate, _admin: AdminDep, db: DbDep
) -> EmailTemplateOut:
    template = _get_template_or_404(db, template_id)
    updates = payload.model_dump(exclude_unset=True)
    if "body_json" in updates:
        updates["body_json"] = json.dumps(updates["body_json"])
    for field, value in updates.items():
        setattr(template, field, value)
    db.add(template)
    db.commit()
    db.refresh(template)
    return _template_out(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    template = _get_template_or_404(db, template_id)
    db.delete(template)
    db.commit()
