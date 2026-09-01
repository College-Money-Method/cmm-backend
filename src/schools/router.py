"""Schools CRUD endpoints."""

import uuid

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from src.analytics.group_identify import identify_school_group
from src.auth.deps import AdminDep, CounselorDep, CurrentUserDep
from src.auth.models import UserRole
from src.config import settings
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.cycles.models import Cycle
from src.schools.models import Contact, School, SchoolEnrollmentCycle
from src.schools.logo_thumbnail import generate_logo_thumbnail
from src.schools.slug_utils import find_slug_owner, unique_slug_db, validate_custom_slug
from src.storage.asset_url import s3_object_url, to_cdn_url
from src.storage.s3_client import S3ClientDep

# Logo objects use unique (uuid) keys, so they are immutable and long-cacheable.
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
from src.content.models import GradeSet
from src.schools.schemas import (
    SchoolCreate,
    SchoolDetail,
    SchoolEnrollmentCycleOut,
    SchoolEnrollmentCycleUpdate,
    SchoolGradeSetUpdate,
    SchoolListItem,
    SchoolListResponse,
    SchoolListVersion,
    SchoolPasswordUpdate,
    SchoolPasswordVerify,
    SchoolPublic,
    SchoolPublicListResponse,
    SchoolSyncResult,
    SchoolUpdate,
    CounselorPublicOut,
)

router = APIRouter(prefix="/api/v1/schools", tags=["schools"])

# Shown to unauthenticated visitors when a school slug/id is missing OR belongs
# to a non-customer. Identical for both cases so the response never reveals that
# a non-partnered school exists in the database.
_SCHOOL_NOT_FOUND_DETAIL = "We couldn't find your partnered school, contact College Money Method"


def _find_public_school(db, *, slug: str | None = None, school_id: uuid.UUID | None = None) -> School:
    """Fetch a public-accessible school for a public (no-auth) endpoint.

    Access is granted to current customers OR prospects an admin has explicitly
    activated (is_cmm_website_activated) so they can be shared a preview link.
    Everything else raises the same 404 as a missing school so non-activated
    prospects can't be reached by direct URL.
    """
    q = db.query(School).filter(
        or_(
            School.is_current_customer.is_(True),
            School.is_cmm_website_activated.is_(True),
        )
    )
    if slug is not None:
        q = q.filter(or_(School.slug == slug, School.airtable_slug == slug))
    if school_id is not None:
        q = q.filter(School.id == school_id)
    school = q.first()
    if not school:
        raise HTTPException(status_code=404, detail=_SCHOOL_NOT_FOUND_DETAIL)
    return school


# Self-reported per-grade enrollment; enrollment_9_12 is recomputed as their
# sum whenever any of these change (see update_school)
_ENROLLMENT_GRADE_FIELDS = (
    "enrollment_grade_9",
    "enrollment_grade_10",
    "enrollment_grade_11",
    "enrollment_grade_12",
)


@router.get("/slug/{slug}/counselors", response_model=list[CounselorPublicOut])
def get_school_counselors_public(
    slug: str,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> list[CounselorPublicOut]:
    """Return counselors assigned to a school (public, no auth required)."""
    school = _find_public_school(db, slug=slug)

    roles = (
        db.query(UserRole)
        .filter(UserRole.school_id == school.id, UserRole.role.in_(["hub_admin", "hub_user"]))
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
    """Enforce hub user scope: hub users may only access their own school."""
    if user.role in ("hub_admin", "hub_user") and user.school_id != school_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to your own school",
        )


# ── Literal routes MUST come before /{school_id} ──────────────────────────────


@router.post("/sync-airtable", response_model=SchoolSyncResult)
def sync_schools_airtable(_admin: AdminDep, db: DbDep, supabase=Depends(get_supabase)) -> SchoolSyncResult:
    """Admin: create new schools, contacts, and counselor auth accounts from Airtable."""
    from src.schools.sync import sync_schools_contacts_from_airtable
    return sync_schools_contacts_from_airtable(db, supabase)


@router.get("/list-version", response_model=SchoolListVersion)
def get_school_list_version(db: DbDep, user: CurrentUserDep) -> SchoolListVersion:
    """Cheap fingerprint of the whole school list for client cache invalidation.

    Combines row count (covers create/delete) with the newest modification
    timestamp (covers edits via ``onupdate``). The frontend caches the list plus
    this version and refetches only when the version changes.
    """
    count, last_modified = db.execute(
        select(
            func.count(School.id),
            func.max(func.coalesce(School.updated_at, School.created_at)),
        )
    ).one()
    stamp = int(last_modified.timestamp()) if last_modified else 0
    return SchoolListVersion(version=f"{count}:{stamp}")


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
    school = _find_public_school(db, slug=slug)
    return SchoolPublic.model_validate(school)


@router.post("/slug/{slug}/verify-password", status_code=status.HTTP_200_OK)
def verify_school_password(slug: str, body: SchoolPasswordVerify, db: DbDep) -> dict:
    """Verify the school portal password. Returns 200 + school data if correct, 401 if wrong."""
    school = _find_public_school(db, slug=slug)
    if school.cmm_website_password != body.password:
        raise HTTPException(status_code=401, detail="Incorrect password")
    return {"school": SchoolPublic.model_validate(school).model_dump(mode="json")}


@router.get("/{school_id}/public", response_model=SchoolPublic)
def get_school_public(school_id: uuid.UUID, db: DbDep) -> SchoolPublic:
    """Get a school by UUID (no auth required). Returns safe public fields only."""
    school = _find_public_school(db, school_id=school_id)
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
    limit: int = Query(default=50, ge=1, le=1000),
) -> SchoolListResponse:
    """List schools with optional filters. Hub users are redirected to their own school."""
    # Hub users: return only their school
    if user.role in ("hub_admin", "hub_user"):
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


@router.get("/slug-available")
def check_slug_available(
    _admin: AdminDep,
    db: DbDep,
    slug: str = Query(min_length=1),
    exclude_id: uuid.UUID | None = Query(default=None),
) -> dict:
    """Admin: report whether *slug* is usable, for live validation in the school form.

    Registered ahead of /{school_id} so the literal path isn't parsed as a UUID.
    `exclude_id` lets the edit form ignore the school's own current slug.
    """
    try:
        normalized = validate_custom_slug(slug)
    except ValueError as exc:
        return {"available": False, "slug": None, "reason": str(exc)}

    owner = find_slug_owner(normalized, db, exclude_id=exclude_id)
    if owner:
        return {
            "available": False,
            "slug": normalized,
            "reason": f'Already used by {owner.name}.',
        }
    return {"available": True, "slug": normalized, "reason": None}


def _resolve_slug(value: str, db, exclude_id: uuid.UUID | None = None) -> str:
    """Validate an admin-supplied slug and assert it is free.

    Raises 400 for a malformed/reserved slug and 409 when another school already
    owns it (mirrored by the /slug-available endpoint the admin UI checks).
    """
    try:
        slug = validate_custom_slug(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    owner = find_slug_owner(slug, db, exclude_id=exclude_id)
    if owner:
        raise HTTPException(
            status_code=409,
            detail=f'The slug "{slug}" is already used by {owner.name}.',
        )
    return slug


@router.post("", response_model=SchoolDetail, status_code=status.HTTP_201_CREATED)
def create_school(body: SchoolCreate, _admin: AdminDep, db: DbDep) -> SchoolDetail:
    """Create a new school (admin only)."""
    data = body.model_dump(exclude_none=True)
    # An explicit slug wins; otherwise derive a free one from the name.
    if data.get("slug"):
        data["slug"] = _resolve_slug(data["slug"], db)
    else:
        data["slug"] = unique_slug_db(body.name, db)
    # List views render logo_thumb_url; use the full logo until a real thumb exists
    if data.get("logo_url"):
        data["logo_thumb_url"] = data["logo_url"]
    school = School(**data)
    db.add(school)
    db.commit()
    db.refresh(school)
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school.id)
        .one()
    )
    # Keep PostHog "school" group props in sync (fire-and-forget)
    identify_school_group(school)
    return SchoolDetail.model_validate(school)


# ── Single-resource endpoints ──────────────────────────────────────────────────


@router.post("/{school_id}/logo")
async def upload_school_logo(
    school_id: uuid.UUID,
    file: UploadFile,
    user: CounselorDep,
    db: DbDep,
    s3: S3ClientDep,
) -> dict:
    """Upload/replace a school's logo. Accessible by hub admin users and super_admin."""
    _check_school_access(school_id, user)
    if user.role not in ("super_admin", "hub_admin"):
        raise HTTPException(status_code=403, detail="Hub admin access required to upload logo")

    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml"}
    if not file.content_type or file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Invalid file type. Allowed: jpeg, png, gif, webp, svg")

    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "png"
    key = f"uploads/school-logos/{school_id}/{uuid.uuid4()}.{ext}"

    content = await file.read()
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=content,
        ContentType=file.content_type,
        CacheControl=_IMMUTABLE_CACHE_CONTROL,
    )
    url = s3_object_url(key)

    # Generate a small webp thumbnail for list views; fall back to the
    # full-size URL when the image can't be rasterized (e.g. SVG)
    thumb_url = url
    thumb_bytes = generate_logo_thumbnail(content)
    if thumb_bytes:
        thumb_key = f"uploads/school-logos/{school_id}/{uuid.uuid4()}-thumb.webp"
        s3.put_object(
            Bucket=settings.s3_bucket_name,
            Key=thumb_key,
            Body=thumb_bytes,
            ContentType="image/webp",
            CacheControl=_IMMUTABLE_CACHE_CONTROL,
        )
        thumb_url = s3_object_url(thumb_key)

    school = db.get(School, school_id)
    if school:
        school.logo_url = url
        school.logo_thumb_url = thumb_url
        db.commit()

    return {"url": to_cdn_url(url)}


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
    # Require hub admin permission to update
    if user.role not in ("super_admin", "hub_admin"):
        raise HTTPException(status_code=403, detail="Hub admin access required to update school")

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    update_data = body.model_dump(exclude_unset=True)

    # Hub admins may only update a safe subset of fields; name and slug are
    # excluded because the public URL must stay stable for them
    if user.role == "hub_admin":
        counselor_allowed = {
            "logo_url", "nickname",
            "city", "state", "zip_code", "street_address",
            "appointlet_link", "calendar_link",
            "enrollment_9_12", *_ENROLLMENT_GRADE_FIELDS,
        }
        update_data = {k: v for k, v in update_data.items() if k in counselor_allowed}

    # Slug drives the public SRC URL — validate and uniqueness-check before it
    # reaches setattr. Sending an empty value regenerates it from the name.
    if "slug" in update_data:
        raw = (update_data["slug"] or "").strip()
        update_data["slug"] = (
            _resolve_slug(raw, db, exclude_id=school_id)
            if raw
            else unique_slug_db(update_data.get("name") or school.name, db, exclude_id=school_id)
        )

    # Keep the list-view thumbnail in sync when the logo changes outside the
    # dedicated upload endpoint (which generates a real thumbnail itself)
    if "logo_url" in update_data and update_data["logo_url"] != school.logo_url:
        update_data["logo_thumb_url"] = update_data["logo_url"]

    for field, value in update_data.items():
        setattr(school, field, value)

    # Per-grade values are the source of truth for the total when present —
    # enrollment_9_12 feeds the % reach metric and the enrollment_range
    # computed column, so keep it in sync on any per-grade change
    if any(f in update_data for f in _ENROLLMENT_GRADE_FIELDS):
        grades = [getattr(school, f) for f in _ENROLLMENT_GRADE_FIELDS]
        if any(g is not None for g in grades):
            school.enrollment_9_12 = sum(g for g in grades if g is not None)

    db.commit()
    school = (
        db.query(School)
        .options(joinedload(School.cohort), joinedload(School.grade_set), selectinload(School.contacts))
        .filter(School.id == school_id)
        .one()
    )
    # Keep PostHog "school" group props in sync (fire-and-forget)
    identify_school_group(school)
    return SchoolDetail.model_validate(school)


def _enrollment_total(grades: list[int | None]) -> int | None:
    """Sum of the reported per-grade values; None when none are reported."""
    present = [g for g in grades if g is not None]
    return sum(present) if present else None


def _enrollment_cycle_out(
    cycle: Cycle, grades: list[int | None]
) -> SchoolEnrollmentCycleOut:
    return SchoolEnrollmentCycleOut(
        cycle_id=cycle.id,
        cycle_name=cycle.name,
        is_current=cycle.is_current,
        beginning_date=cycle.beginning_date,
        end_date=cycle.end_date,
        enrollment_grade_9=grades[0],
        enrollment_grade_10=grades[1],
        enrollment_grade_11=grades[2],
        enrollment_grade_12=grades[3],
        enrollment_9_12=_enrollment_total(grades),
    )


@router.get("/{school_id}/enrollment-cycles", response_model=list[SchoolEnrollmentCycleOut])
def list_school_enrollment_cycles(
    school_id: uuid.UUID,
    db: DbDep,
    user: CurrentUserDep,
) -> list[SchoolEnrollmentCycleOut]:
    """Per-cycle enrollment for a school, newest cycle first.

    Current cycle values come from the schools columns; all other cycles from
    school_enrollment_cycles (null grades where nothing has been reported).
    """
    _check_school_access(school_id, user)
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    cycles = (
        db.query(Cycle)
        .order_by(Cycle.beginning_date.desc().nullslast(), Cycle.name.desc())
        .all()
    )
    history = {
        ec.cycle_id: ec
        for ec in db.query(SchoolEnrollmentCycle)
        .filter(SchoolEnrollmentCycle.school_id == school_id)
        .all()
    }

    out: list[SchoolEnrollmentCycleOut] = []
    for cycle in cycles:
        if cycle.is_current:
            grades = [getattr(school, f) for f in _ENROLLMENT_GRADE_FIELDS]
        else:
            ec = history.get(cycle.id)
            grades = [getattr(ec, f) if ec else None for f in _ENROLLMENT_GRADE_FIELDS]
        out.append(_enrollment_cycle_out(cycle, grades))
    return out


@router.put("/{school_id}/enrollment-cycles/{cycle_id}", response_model=SchoolEnrollmentCycleOut)
def upsert_school_enrollment_cycle(
    school_id: uuid.UUID,
    cycle_id: uuid.UUID,
    body: SchoolEnrollmentCycleUpdate,
    db: DbDep,
    user: CurrentUserDep,
) -> SchoolEnrollmentCycleOut:
    """Save enrollment for one cycle. Editing the current cycle writes the schools
    columns directly (keeping enrollment_9_12 / enrollment_range analytics in
    sync); other cycles are upserted into school_enrollment_cycles."""
    _check_school_access(school_id, user)
    if user.role not in ("super_admin", "hub_admin"):
        raise HTTPException(status_code=403, detail="Hub admin access required to update enrollment")

    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    cycle = db.query(Cycle).filter(Cycle.id == cycle_id).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")

    grades = [
        body.enrollment_grade_9,
        body.enrollment_grade_10,
        body.enrollment_grade_11,
        body.enrollment_grade_12,
    ]

    if cycle.is_current:
        for field, value in zip(_ENROLLMENT_GRADE_FIELDS, grades):
            setattr(school, field, value)
        # Keep the total in sync — it feeds % reach and the enrollment_range column
        school.enrollment_9_12 = _enrollment_total(grades)
        db.commit()
        identify_school_group(school)
    else:
        row = (
            db.query(SchoolEnrollmentCycle)
            .filter(
                SchoolEnrollmentCycle.school_id == school_id,
                SchoolEnrollmentCycle.cycle_id == cycle_id,
            )
            .first()
        )
        if row is None:
            row = SchoolEnrollmentCycle(school_id=school_id, cycle_id=cycle_id)
            db.add(row)
        for field, value in zip(_ENROLLMENT_GRADE_FIELDS, grades):
            setattr(row, field, value)
        db.commit()

    return _enrollment_cycle_out(cycle, grades)


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
