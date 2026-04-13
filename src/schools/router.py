"""Schools CRUD endpoints."""

import uuid

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from src.auth.deps import AdminDep, CurrentUserDep
from src.auth.models import UserRole
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.schools.models import Contact, School
from src.content.models import GradeSet
from src.schools.schemas import (
    SchoolCreate,
    SchoolDetail,
    SchoolGradeSetUpdate,
    SchoolListItem,
    SchoolListResponse,
    SchoolPasswordUpdate,
    SchoolPasswordVerify,
    SchoolPublic,
    SchoolPublicListResponse,
    SchoolUpdate,
    CounselorPublicOut,
)

router = APIRouter(prefix="/api/v1/schools", tags=["schools"])


@router.get("/slug/{slug}/counselors", response_model=list[CounselorPublicOut])
def get_school_counselors_public(
    slug: str,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> list[CounselorPublicOut]:
    """Return counselors assigned to a school (public, no auth required)."""
    school = db.query(School).filter(School.slug == slug).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    roles = (
        db.query(UserRole)
        .filter(UserRole.school_id == school.id, UserRole.role == "counselor")
        .all()
    )

    result: list[CounselorPublicOut] = []
    for role_record in roles:
        try:
            resp = supabase.auth.admin.get_user_by_id(str(role_record.user_id))
            if resp and resp.user:
                meta = resp.user.user_metadata or {}
                first = meta.get("first_name") or ""
                last = meta.get("last_name") or ""
                result.append(CounselorPublicOut(
                    first_name=first or None,
                    last_name=last or None,
                    full_name=f"{first} {last}".strip() or None,
                    title=role_record.title or f"{school.name} Counselor",
                    email=resp.user.email or None,
                ))
        except Exception:
            pass
    return result


def _check_school_access(school_id: uuid.UUID, user: CurrentUserDep) -> None:
    """Enforce counselor scope: counselors may only access their own school."""
    if user.role == "counselor" and user.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to your own school",
        )


# ── Literal routes MUST come before /{school_id} ──────────────────────────────


@router.get("/states", response_model=list[str])
def list_states(db: DbDep, user: CurrentUserDep) -> list[str]:
    """Return distinct non-null states, sorted."""
    rows = (
        db.execute(
            select(School.state)
            .where(School.state.isnot(None))
            .distinct()
            .order_by(School.state)
        )
        .scalars()
        .all()
    )
    return list(rows)


@router.get("/cities", response_model=list[str])
def list_cities(
    db: DbDep,
    user: CurrentUserDep,
    state: str | None = Query(default=None),
) -> list[str]:
    """Return distinct non-null cities, optionally filtered by state."""
    q = select(School.city).where(School.city.isnot(None)).distinct().order_by(School.city)
    if state:
        q = q.where(School.state == state)
    return list(db.execute(q).scalars().all())


# ── Public (no-auth) endpoints ────────────────────────────────────────────────


@router.get("/public", response_model=SchoolPublicListResponse)
def list_schools_public(
    db: DbDep,
    search: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> SchoolPublicListResponse:
    """List current-customer schools for the public discovery page (no auth)."""
    q = db.query(School).filter(School.is_current_customer.is_(True))
    if search:
        term = f"%{search}%"
        q = q.filter((School.name.ilike(term)) | (School.city.ilike(term)))
    total = q.count()
    schools = q.order_by(School.name).offset(skip).limit(limit).all()
    return SchoolPublicListResponse(
        items=[SchoolPublic.model_validate(s) for s in schools],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/slug/{slug}", response_model=SchoolPublic)
def get_school_by_slug(slug: str, db: DbDep) -> SchoolPublic:
    """Get a school by slug (no auth required). Returns safe public fields only."""
    school = db.query(School).filter(School.slug == slug).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return SchoolPublic.model_validate(school)


@router.post("/slug/{slug}/verify-password", status_code=status.HTTP_200_OK)
def verify_school_password(slug: str, body: SchoolPasswordVerify, db: DbDep) -> dict:
    """Verify the school portal password. Returns 200 + school data if correct, 401 if wrong."""
    school = db.query(School).filter(School.slug == slug).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if school.cmm_website_password != body.password:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"school": SchoolPublic.model_validate(school).model_dump(mode="json")}


@router.get("/{school_id}/public", response_model=SchoolPublic)
def get_school_public(school_id: uuid.UUID, db: DbDep) -> SchoolPublic:
    """Get a school by UUID (no auth required). Returns safe public fields only."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return SchoolPublic.model_validate(school)


# ── Collection endpoints ───────────────────────────────────────────────────────


def _build_order_by(sort_by: str, sort_dir: str):
    """Return an order_by clause list for the given sort parameters."""
    desc = sort_dir == "desc"
    if sort_by == "state":
        state_col = School.state.desc() if desc else School.state
        return [state_col, School.name]
    if sort_by == "enrollment":
        col = School.enrollment_9_12.desc().nullslast() if desc else School.enrollment_9_12.asc().nullslast()
        return [col]
    # default: name
    return [School.name.desc() if desc else School.name]


@router.get("", response_model=SchoolListResponse)
def list_schools(
    db: DbDep,
    user: CurrentUserDep,
    search: str | None = Query(default=None),
    state: str | None = Query(default=None),
    city: str | None = Query(default=None),
    cohort_ids: list[uuid.UUID] | None = Query(default=None),
    is_current_customer: bool | None = Query(default=None),
    enrollment_range: str | None = Query(default=None),
    sort_by: Literal["name", "state", "enrollment"] = Query(default="name"),
    sort_dir: Literal["asc", "desc"] = Query(default="asc"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> SchoolListResponse:
    """List schools with optional filters. Counselors are redirected to their own school."""
    # Counselors: return only their school
    if user.role == "counselor":
        if user.school_id is None:
            return SchoolListResponse(items=[], total=0, skip=0, limit=limit)
        school = (
            db.query(School)
            .options(joinedload(School.cohort))
            .filter(School.id == user.school_id)
            .first()
        )
        items = [SchoolListItem.model_validate(school)] if school else []
        return SchoolListResponse(items=items, total=len(items), skip=0, limit=limit)

    q = db.query(School).options(joinedload(School.cohort))

    if search:
        term = f"%{search}%"
        q = q.filter((School.name.ilike(term)) | (School.city.ilike(term)))
    if state:
        q = q.filter(School.state == state)
    if city:
        q = q.filter(School.city == city)
    if cohort_ids:
        q = q.filter(School.cohort_id.in_(cohort_ids))
    if is_current_customer is not None:
        q = q.filter(School.is_current_customer == is_current_customer)
    if enrollment_range:
        q = q.filter(School.enrollment_range == enrollment_range)

    total = q.count()
    schools = q.order_by(*_build_order_by(sort_by, sort_dir)).offset(skip).limit(limit).all()

    return SchoolListResponse(
        items=[SchoolListItem.model_validate(s) for s in schools],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post("", response_model=SchoolDetail, status_code=status.HTTP_201_CREATED)
def create_school(body: SchoolCreate, _admin: AdminDep, db: DbDep) -> SchoolDetail:
    """Create a new school (admin only)."""
    school = School(**body.model_dump(exclude_none=True))
    db.add(school)
    db.commit()
    db.refresh(school)
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school.id)
        .one()
    )
    return SchoolDetail.model_validate(school)


# ── Single-resource endpoints ──────────────────────────────────────────────────


@router.get("/{school_id}", response_model=SchoolDetail)
def get_school(school_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> SchoolDetail:
    _check_school_access(school_id, user)
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school_id)
        .first()
    )
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return SchoolDetail.model_validate(school)


@router.patch("/{school_id}", response_model=SchoolDetail)
def update_school(
    school_id: uuid.UUID,
    body: SchoolUpdate,
    db: DbDep,
    user: CurrentUserDep,
) -> SchoolDetail:
    _check_school_access(school_id, user)
    # Viewers cannot write
    if user.role == "viewer":
        raise HTTPException(status_code=403, detail="Read-only access")

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    update_data = body.model_dump(exclude_unset=True)

    # Counselors may only update a safe subset of fields
    if user.role == "counselor":
        counselor_allowed = {
            "city", "state", "zip_code", "street_address",
            "appointlet_link", "calendar_link",
        }
        update_data = {k: v for k, v in update_data.items() if k in counselor_allowed}

    for field, value in update_data.items():
        setattr(school, field, value)

    db.commit()
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school_id)
        .one()
    )
    return SchoolDetail.model_validate(school)


@router.delete("/{school_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_school(school_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    """Delete a school (admin only)."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    db.delete(school)
    db.commit()


@router.patch("/{school_id}/password", status_code=status.HTTP_200_OK)
def update_school_password(
    school_id: uuid.UUID,
    body: SchoolPasswordUpdate,
    _admin: AdminDep,
    db: DbDep,
) -> dict:
    """Set/reset the shared school password for student/family portal access (admin only)."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    school.cmm_website_password = body.password
    db.commit()
    return {"message": "Password updated successfully"}


@router.put("/{school_id}/grade-set", response_model=SchoolDetail)
def assign_grade_set(
    school_id: uuid.UUID,
    body: SchoolGradeSetUpdate,
    _admin: AdminDep,
    db: DbDep,
) -> SchoolDetail:
    """Admin: assign or clear a grade set for a school."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    if body.grade_set_id is not None:
        gs = db.get(GradeSet, body.grade_set_id)
        if not gs:
            raise HTTPException(status_code=404, detail="Grade set not found")
    school.grade_set_id = body.grade_set_id
    db.commit()
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school_id)
        .one()
    )
    return SchoolDetail.model_validate(school)
