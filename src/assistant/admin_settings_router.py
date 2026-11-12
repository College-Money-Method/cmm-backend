"""Admin assistant settings: prompt sections and which schools get the counselor assistant.

Prompt sections are admin-written instructions appended to the system prompt
(see ``prompt_sections``). A school's ``counselor_assistant_enabled`` flag
shows the assistant in its Counselor Hub and lets its counselors use it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select

from src.assistant.models import AssistantPromptSection
from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.schools.models import School

router = APIRouter(prefix="/api/v1/admin/assistant", tags=["assistant-admin"])

# Every enabled section is sent with every question, so keep the total bounded.
MAX_PROMPT_SECTIONS = 20
MAX_TITLE_LENGTH = 120
MAX_BODY_LENGTH = 4000

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_LENGTH)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_BODY_LENGTH)]


class PromptSectionIn(BaseModel):
    title: Title
    body: Body
    audience: Literal["family", "counselor", "both"] = "both"
    enabled: bool = True


class PromptSectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    body: str
    audience: str
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CounselorSchoolIn(BaseModel):
    enabled: bool


class CounselorSchoolOut(BaseModel):
    id: uuid.UUID
    counselor_assistant_enabled: bool


def _section(db, section_id: uuid.UUID) -> AssistantPromptSection:
    row = db.get(AssistantPromptSection, section_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prompt_section_not_found")
    return row


@router.get("/prompt-sections", response_model=list[PromptSectionOut])
def list_prompt_sections(_admin: AdminDep, db: DbDep) -> list[AssistantPromptSection]:
    """Every section, in the order they are added to the prompt."""
    return list(
        db.scalars(
            select(AssistantPromptSection).order_by(AssistantPromptSection.created_at, AssistantPromptSection.id)
        )
    )


@router.post("/prompt-sections", response_model=PromptSectionOut, status_code=status.HTTP_201_CREATED)
def create_prompt_section(body: PromptSectionIn, _admin: AdminDep, db: DbDep) -> AssistantPromptSection:
    count = db.scalar(select(func.count()).select_from(AssistantPromptSection)) or 0
    if count >= MAX_PROMPT_SECTIONS:
        raise HTTPException(status_code=409, detail="prompt_section_limit_reached")
    row = AssistantPromptSection(**body.model_dump(), created_at=datetime.now(UTC))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/prompt-sections/{section_id}", response_model=PromptSectionOut)
def update_prompt_section(
    section_id: uuid.UUID, body: PromptSectionIn, _admin: AdminDep, db: DbDep
) -> AssistantPromptSection:
    row = _section(db, section_id)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    row.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/prompt-sections/{section_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_prompt_section(section_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> Response:
    db.delete(_section(db, section_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/counselor-schools/{school_id}", response_model=CounselorSchoolOut)
def set_counselor_school(school_id: uuid.UUID, body: CounselorSchoolIn, _admin: AdminDep, db: DbDep) -> CounselorSchoolOut:
    """Turn the counselor assistant on or off in one school's Counselor Hub."""
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="school_not_found")
    school.counselor_assistant_enabled = body.enabled
    db.commit()
    return CounselorSchoolOut(id=school.id, counselor_assistant_enabled=school.counselor_assistant_enabled)
