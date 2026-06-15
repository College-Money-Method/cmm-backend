"""Communication template endpoints — browse and manage standalone educational email templates."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from src.auth.deps import AdminDep, CounselorDep
from src.auth.schemas import CurrentUser
from src.db.deps import DbDep
from src.communications.models import CommunicationTemplate
from src.communications.schemas import (
    CommunicationTemplateCreate,
    CommunicationTemplateOut,
    CommunicationTemplateUpdate,
)

router = APIRouter(prefix="/api/v1/communications", tags=["communications"])


def _validate_format(fmt: str, content: str | None, google_docs_url: str | None) -> None:
    """Raise 422 if required field for the given format is missing."""
    if fmt == "google_docs" and not google_docs_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="google_docs_url is required when format is 'google_docs'",
        )
    if fmt == "rich_text" and not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="content is required when format is 'rich_text'",
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/", response_model=list[CommunicationTemplateOut])
def list_templates(db: DbDep, _user: CounselorDep) -> list[CommunicationTemplate]:
    """List all active templates ordered by sort_order asc, created_at asc."""
    rows = (
        db.execute(
            select(CommunicationTemplate)
            .where(CommunicationTemplate.is_active.is_(True))
            .order_by(CommunicationTemplate.sort_order.asc(), CommunicationTemplate.created_at.asc())
        )
        .scalars()
        .all()
    )
    return list(rows)


@router.get("/{template_id}", response_model=CommunicationTemplateOut)
def get_template(
    template_id: uuid.UUID,
    db: DbDep,
    user: CounselorDep,
) -> CommunicationTemplate:
    """Get one template. Admins can see inactive templates; counselors cannot."""
    stmt = select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    template = db.execute(stmt).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    # Non-admins cannot see inactive templates
    if not template.is_active and user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    return template


@router.post("/", response_model=CommunicationTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    body: CommunicationTemplateCreate,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Create a new communication template (admin only)."""
    _validate_format(body.format, body.content, body.google_docs_url)

    template = CommunicationTemplate(
        name=body.name,
        description=body.description,
        subject=body.subject,
        format=body.format,
        content=body.content,
        google_docs_url=body.google_docs_url,
        sort_order=body.sort_order,
        updated_at=None,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.patch("/{template_id}/toggle", response_model=CommunicationTemplateOut)
def toggle_template(
    template_id: uuid.UUID,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Flip is_active on a template (admin only). Must be defined before PATCH /{template_id}."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    template.is_active = not template.is_active
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


@router.patch("/{template_id}", response_model=CommunicationTemplateOut)
def update_template(
    template_id: uuid.UUID,
    body: CommunicationTemplateUpdate,
    db: DbDep,
    _user: AdminDep,
) -> CommunicationTemplate:
    """Partial update of a communication template (admin only)."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    # Apply only explicitly set fields
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(template, field, value)

    # Re-validate format consistency if format or content/url changed
    effective_format = updates.get("format", template.format)
    effective_content = updates.get("content", template.content)
    effective_url = updates.get("google_docs_url", template.google_docs_url)
    _validate_format(effective_format, effective_content, effective_url)

    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: uuid.UUID,
    db: DbDep,
    _user: AdminDep,
) -> None:
    """Hard delete a communication template (admin only)."""
    template = db.execute(
        select(CommunicationTemplate).where(CommunicationTemplate.id == template_id)
    ).scalars().first()

    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    db.delete(template)
    db.commit()
