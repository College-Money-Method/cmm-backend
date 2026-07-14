"""Pydantic schemas for auth/role management."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr


class UserRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None
    school_role: str | None = None  # cosmetic Airtable title, e.g. "Director"


class CurrentUser(BaseModel):
    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None
    school_role: str | None = None  # cosmetic Airtable title, e.g. "Director"


class CounselorCreate(BaseModel):
    email: EmailStr
    # Optional: directors can "just add the email" — name defaults to the email handle
    first_name: str | None = None
    last_name: str | None = None
    # Optional: hub_admin (director) creations are forced to the director's own school
    school_id: uuid.UUID | None = None
    role: Literal["hub_admin", "hub_user", "viewer"] = "hub_user"
    title: str | None = None
    # If provided, used as initial password. Otherwise defaults to the email handle +
    # the school's resource-center password (falls back to Supabase invite if unset).
    password: str | None = None


class CounselorUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    school_id: uuid.UUID | None = None
    role: Literal["hub_admin", "hub_user", "viewer"] | None = None
    title: str | None = None


class CounselorOut(BaseModel):
    user_id: uuid.UUID
    email: str
    first_name: str | None
    last_name: str | None
    full_name: str | None
    role: str
    school_id: uuid.UUID | None
    school_name: str | None
    title: str | None = None
    school_role: str | None = None


class CounselorListResponse(BaseModel):
    items: list[CounselorOut]
    total: int
    skip: int
    limit: int


class CounselorSyncResult(BaseModel):
    contacts_created: int = 0
    contacts_updated: int = 0
    contacts_deactivated: int = 0
    contacts_reactivated: int = 0
    email_collisions: int = 0
    counselors_created: int
    school_roles_updated: int
    counselors_revoked: int = 0
    drift_relinked: int = 0
    skipped: int
    synced_at: str
