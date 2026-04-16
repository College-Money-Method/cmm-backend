"""Cohorts CRUD endpoints."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from src.auth.deps import AdminDep, CurrentUserDep
from src.cycles.models import Cohort, Cycle
from src.cycles.schemas import CohortCreate, CohortOut, CohortUpdate, CohortWithSchools, CohortWithSchoolsResponse, CycleOut
from src.db.deps import DbDep
from src.schools.models import School
from src.schools.router import _build_order_by
from src.schools.schemas import SchoolListItem

router = APIRouter(prefix="/api/v1/cohorts", tags=["cohorts"])


@router.get("/cycles", response_model=list[CycleOut])
def list_cycles(db: DbDep, _user: CurrentUserDep) -> list[CycleOut]:
    """List all cycles."""
    return db.query(Cycle).order_by(Cycle.name).all()


@router.get("/schools", response_model=CohortWithSchoolsResponse)
def list_cohorts_with_schools(
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
    limit: int = Query(default=5, ge=1, le=1000),
) -> CohortWithSchoolsResponse:
    """Return cohorts with their matching schools embedded, paginated by cohort.
    A 'No Cohort' group is appended last if any matching schools have no cohort.
    """
    # Build a base school query with all filters applied
    def _apply_filters(q):
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
        return q

    # Find cohort_ids that have at least one matching school
    matching_cohort_ids_q = _apply_filters(
        db.query(School.cohort_id).filter(School.cohort_id.isnot(None))
    ).distinct()
    matching_cohort_ids = [row[0] for row in matching_cohort_ids_q.all()]

    # Check for any matching "No Cohort" schools
    has_no_cohort = _apply_filters(
        db.query(School.id).filter(School.cohort_id.is_(None))
    ).first() is not None

    # Paginate cohorts by name order
    cohorts_q = db.query(Cohort).filter(Cohort.id.in_(matching_cohort_ids)).order_by(Cohort.name)
    total_named_cohorts = cohorts_q.count()
    total = total_named_cohorts + (1 if has_no_cohort else 0)

    page_cohorts = cohorts_q.offset(skip).limit(limit).all()

    order_by = _build_order_by(sort_by, sort_dir)

    # Fetch schools for each cohort on this page
    items: list[CohortWithSchools] = []
    for cohort in page_cohorts:
        schools = (
            _apply_filters(
                db.query(School).options(joinedload(School.cohort)).filter(School.cohort_id == cohort.id)
            )
            .order_by(*order_by)
            .all()
        )
        items.append(
            CohortWithSchools(
                cohort_id=cohort.id,
                cohort_name=cohort.name,
                schools=[SchoolListItem.model_validate(s) for s in schools],
            )
        )

    # Include "No Cohort" group if it falls on this page (always last)
    no_cohort_index = total_named_cohorts  # 0-based position among all groups
    if has_no_cohort and skip <= no_cohort_index < skip + limit:
        no_cohort_schools = (
            _apply_filters(
                db.query(School).options(joinedload(School.cohort)).filter(School.cohort_id.is_(None))
            )
            .order_by(*order_by)
            .all()
        )
        items.append(
            CohortWithSchools(
                cohort_id=None,
                cohort_name="No Cohort",
                schools=[SchoolListItem.model_validate(s) for s in no_cohort_schools],
            )
        )

    return CohortWithSchoolsResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("", response_model=list[CohortOut])
def list_cohorts(db: DbDep, user: CurrentUserDep) -> list[CohortOut]:
    """List all cohorts with school counts."""
    cohorts = db.query(Cohort).order_by(Cohort.name).all()
    # Build school count map
    school_counts = dict(
        db.query(School.cohort_id, func.count(School.id))
        .filter(School.cohort_id.isnot(None))
        .group_by(School.cohort_id)
        .all()
    )
    results = []
    for cohort in cohorts:
        out = CohortOut.model_validate(cohort)
        out.school_count = school_counts.get(cohort.id, 0)
        results.append(out)
    return results


@router.post("", response_model=CohortOut, status_code=status.HTTP_201_CREATED)
def create_cohort(body: CohortCreate, _admin: AdminDep, db: DbDep) -> CohortOut:
    """Create a cohort (admin only)."""
    existing = db.query(Cohort).filter(Cohort.name == body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="A cohort with this name already exists")
    cohort = Cohort(**body.model_dump())
    db.add(cohort)
    db.commit()
    db.refresh(cohort)
    out = CohortOut.model_validate(cohort)
    out.school_count = 0
    return out


@router.patch("/{cohort_id}", response_model=CohortOut)
def update_cohort(
    cohort_id: uuid.UUID,
    body: CohortUpdate,
    _admin: AdminDep,
    db: DbDep,
) -> CohortOut:
    """Update a cohort (admin only)."""
    cohort = db.query(Cohort).filter(Cohort.id == cohort_id).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")

    update_data = body.model_dump(exclude_unset=True)
    if "name" in update_data:
        dup = (
            db.query(Cohort)
            .filter(Cohort.name == update_data["name"], Cohort.id != cohort_id)
            .first()
        )
        if dup:
            raise HTTPException(status_code=409, detail="A cohort with this name already exists")

    for field, value in update_data.items():
        setattr(cohort, field, value)

    db.commit()
    db.refresh(cohort)

    count = db.query(func.count(School.id)).filter(School.cohort_id == cohort_id).scalar() or 0
    out = CohortOut.model_validate(cohort)
    out.school_count = count
    return out


@router.delete("/{cohort_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cohort(cohort_id: uuid.UUID, _admin: AdminDep, db: DbDep) -> None:
    """Delete a cohort (admin only). Schools in this cohort will have cohort_id set to NULL."""
    cohort = db.query(Cohort).filter(Cohort.id == cohort_id).first()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")
    # Nullify school cohort references before deleting
    db.query(School).filter(School.cohort_id == cohort_id).update(
        {School.cohort_id: None}, synchronize_session=False
    )
    db.delete(cohort)
    db.commit()
