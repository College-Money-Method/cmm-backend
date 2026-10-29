"""Admin (super_admin) College Aid manual corrections: add/edit colleges and per-year records."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from src.auth.deps import AdminDep
from src.colleges import edit_service
from src.colleges.admin_schemas import (
    CollegeCreate,
    CollegeDetail,
    CollegeListItem,
    CollegePatch,
    RecordCreate,
    RecordOut,
    RecordPatch,
)
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/colleges/admin", tags=["college-aid-admin"])


@router.post("", response_model=CollegeListItem, status_code=status.HTTP_201_CREATED)
def create_college(body: CollegeCreate, db: DbDep, admin: AdminDep):
    return edit_service.create_college(db, body, admin.user_id)


@router.get("/{college_id}", response_model=CollegeDetail)
def get_college(college_id: uuid.UUID, db: DbDep, _admin: AdminDep):
    return edit_service.get_detail(db, college_id)


@router.patch("/{college_id}", response_model=CollegeListItem)
def patch_college(college_id: uuid.UUID, body: CollegePatch, db: DbDep, admin: AdminDep):
    return edit_service.patch_college(db, college_id, body, admin.user_id)


@router.delete("/{college_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_college(college_id: uuid.UUID, db: DbDep, admin: AdminDep):
    edit_service.delete_college(db, college_id, admin.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{college_id}/records", response_model=RecordOut, status_code=status.HTTP_201_CREATED)
def create_record(college_id: uuid.UUID, body: RecordCreate, db: DbDep, admin: AdminDep):
    return edit_service.create_record(db, college_id, body, admin.user_id)


@router.patch("/records/{record_id}", response_model=RecordOut)
def patch_record(record_id: uuid.UUID, body: RecordPatch, db: DbDep, admin: AdminDep):
    return edit_service.patch_record(db, record_id, body, admin.user_id)


@router.delete("/records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_record(record_id: uuid.UUID, db: DbDep, admin: AdminDep):
    edit_service.delete_record(db, record_id, admin.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
