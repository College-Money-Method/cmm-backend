"""Pydantic schemas for auth/role management."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr


class UserRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None


class CurrentUser(BaseModel):
    user_id: uuid.UUID
    role: Literal["super_admin", "hub_admin", "hub_user", "viewer"]
    school_id: uuid.UUID | None = None


class CounselorCreate(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    school_id: uuid.UUID
    role: Literal["hub_admin", "hub_user", "viewer"] = "hub_user"
    title: str | None = None
    # If provided, used as initial password; otherwise Supabase sends invite email
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
