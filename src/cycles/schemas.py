"""Pydantic schemas for cohorts."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.schools.schemas import SchoolListItem


class CohortOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    hide_unavailability_calendar: bool = False
    created_at: datetime | None = None
    school_count: int = 0


class CohortCreate(BaseModel):
    name: str
    hide_unavailability_calendar: bool = False


class CohortUpdate(BaseModel):
    name: str | None = None
    hide_unavailability_calendar: bool | None = None


class CohortWithSchools(BaseModel):
    cohort_id: uuid.UUID | None
    cohort_name: str
    schools: list[SchoolListItem]


class CohortWithSchoolsResponse(BaseModel):
    items: list[CohortWithSchools]
    total: int
    skip: int
    limit: int
