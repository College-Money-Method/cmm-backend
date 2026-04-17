"""Content management API router."""

from __future__ import annotations

import io
import re
import uuid
from typing import Annotated

import boto3
import requests
from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import contains_eager, selectinload

from src.auth.deps import AdminDep
from src.content.models import (
    AssetType,
    ContentAsset,
    ContentAssetCohort,
    ContentAssetFaq,
    ContentAssetGoal,
    ContentAssetObjective,
    ContentAssetResource,
    WorkshopResource,
    Faq,
    Goal,
    GradeConfig,
    GradeConfigGoal,
    GradeSet,
    Objective,
    ReaderQuestion,
    Topic,
    TopicFaq,
    TopicResource,
)
from src.schools.models import School
from src.content.schemas import (
    AssetTypeCreate,
    AssetTypeOut,
    AssetTypeUpdate,
    ContentAssetCreate,
    ContentAssetDetail,
    ContentAssetListItem,
    ContentAssetListResponse,
    ContentAssetSummary,
    ContentAssetUpdate,
    FaqCreate,
    FaqOut,
    FaqsUpdate,
    FaqUpdate,
    GoalCreate,
    GoalOut,
    GoalUpdate,
    GoalWithTopics,
    GradeConfigCreate,
    GradeConfigGoalsUpdate,
    GradeConfigOut,
    GradeConfigSummary,
    GradeConfigUpdate,
    GradeSetCreate,
    GradeSetSummary,
    GradeSetUpdate,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    ReaderQuestionCreate,
    ReaderQuestionOut,
    RelationshipsUpdate,
    ResourcesUpdate,
    TopicCreate,
    TopicDetail,
    TopicListItem,
    TopicListResponse,
    TopicResourcesUpdate,
    TopicSummary,
    TopicUpdate,
)
from src.config import settings
from src.db.deps import DbDep
from src.utils.tiptap import extract_text

router = APIRouter(prefix="/api/v1/content", tags=["content"])


def _calculate_read_time(content: str | None, summary: str | None = None) -> int | None:
    """Estimate reading time in minutes at ~200 wpm from TipTap JSON or HTML content."""
    combined = " ".join(filter(None, [extract_text(content), extract_text(summary)]))
    if not combined:
        # Fallback: strip HTML tags for plain-HTML content
        raw = (content or "") + " " + (summary or "")
        combined = re.sub(r"<[^>]+>", " ", raw)
    words = len(combined.split())
    return max(1, round(words / 200)) if words > 0 else None

# ── Asset Types ───────────────────────────────────────────────────────────────

@router.get("/asset-types", response_model=list[AssetTypeOut])
def list_asset_types(db: DbDep):
    return db.scalars(select(AssetType).order_by(AssetType.name)).all()


@router.get("/asset-types/public", response_model=list[AssetTypeOut])
def list_asset_types_public(db: DbDep):
    """Public endpoint — list all asset types."""
    return db.scalars(select(AssetType).order_by(AssetType.name)).all()


@router.post("/asset-types", response_model=AssetTypeOut, status_code=status.HTTP_201_CREATED)
def create_asset_type(body: AssetTypeCreate, _admin: AdminDep, db: DbDep):
    existing = db.scalar(select(AssetType).where(AssetType.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Asset type with this name already exists")
    obj = AssetType(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/asset-types/{asset_type_id}", response_model=AssetTypeOut)
def update_asset_type(asset_type_id: uuid.UUID, body: AssetTypeUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(AssetType, asset_type_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Asset type not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/asset-types/{asset_type_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset_type(asset_type_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(AssetType, asset_type_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Asset type not found")
    db.delete(obj)
    db.commit()


# ── Goals (formerly Topics) ──────────────────────────────────────────────────

@router.get("/goals", response_model=list[GoalOut])
def list_goals(db: DbDep):
    """Admin: list all goals (flat list)."""
    goals = (
        db.query(Goal)
        .order_by(Goal.sort_order, Goal.name)
        .all()
    )
    return goals


@router.get("/goals/public", response_model=list[GoalOut])
def list_goals_public(db: DbDep):
    """Public endpoint — list all goals."""
    return db.scalars(select(Goal).order_by(Goal.sort_order, Goal.name)).all()


@router.get("/goals/public/grade/{grade}", response_model=list[GoalWithTopics])
def list_goals_by_grade(grade: int, db: DbDep):
    """Public — return goals for a grade with their published topics and content assets."""
    stmt = (
        select(Goal)
        .options(
            selectinload(Goal.content_assets).selectinload(ContentAsset.asset_type),
            selectinload(Goal.topics),
        )
        .order_by(Goal.sort_order, Goal.name)
    )
    goals = db.scalars(stmt).all()

    grade_str = str(grade)
    result = []
    for goal in goals:
        grades = [g.strip() for g in (goal.suggested_grades or "").split(",") if g.strip()]
        if grade_str not in grades:
            continue
        # Filter to published assets and topics only
        goal.content_assets = [a for a in goal.content_assets if a.status == "published"]
        goal.topics = [t for t in goal.topics if t.status == "published"]
        result.append(goal)
    return result


@router.get("/goals/public/slug/{slug}", response_model=GoalWithTopics)
def get_goal_by_slug(slug: str, db: DbDep):
    """Public — return a single goal by slug with published topics and content assets."""
    stmt = (
        select(Goal)
        .where(Goal.slug == slug)
        .options(
            selectinload(Goal.content_assets).selectinload(ContentAsset.asset_type),
            selectinload(Goal.topics),
        )
    )
    goal = db.scalar(stmt)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal.content_assets = [a for a in goal.content_assets if a.status == "published"]
    goal.topics = [t for t in goal.topics if t.status == "published"]
    return goal


@router.post("/goals", response_model=GoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(body: GoalCreate, _admin: AdminDep, db: DbDep):
    import re as _re

    existing = db.scalar(select(Goal).where(Goal.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Goal with this name already exists")
    data = body.model_dump()
    # Auto-generate slug if not provided
    if not data.get("slug"):
        data["slug"] = _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    # Check slug uniqueness
    if db.scalar(select(Goal).where(Goal.slug == data["slug"])):
        raise HTTPException(status_code=409, detail="Goal with this slug already exists")
    obj = Goal(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/goals/{goal_id}", response_model=GoalOut)
def update_goal(goal_id: uuid.UUID, body: GoalUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Goal, goal_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Goal not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(goal_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Goal, goal_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Goal not found")
    db.delete(obj)
    db.commit()


# ── Topics (content-rich pages) ──────────────────────────────────────────────

def _load_topic_detail(db: DbDep, topic_id: uuid.UUID) -> Topic:
    stmt = (
        select(Topic)
        .where(Topic.id == topic_id)
        .options(
            selectinload(Topic.goal),
            selectinload(Topic.faqs),
            selectinload(Topic.resources).selectinload(ContentAsset.asset_type),
        )
    )
    obj = db.scalar(stmt)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    return obj


@router.get("/topics", response_model=TopicListResponse)
def list_topics(
    db: DbDep,
    _admin: AdminDep,
    search: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_dir: Annotated[str, Query()] = "desc",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Admin: list all topics with pagination and filters."""
    stmt = select(Topic).options(selectinload(Topic.goal))

    if search:
        stmt = stmt.where(Topic.title.ilike(f"%{search}%"))
    if status:
        stmt = stmt.where(Topic.status == status)
    if goal_id:
        stmt = stmt.where(Topic.goal_id == goal_id)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.scalar(count_stmt)

    sort_col = getattr(Topic, sort_by, Topic.created_at)
    if sort_dir == "desc":
        stmt = stmt.order_by(sort_col.desc())
    else:
        stmt = stmt.order_by(sort_col.asc())

    items = db.scalars(stmt.offset(skip).limit(limit)).all()
    return {"items": items, "total": total, "skip": skip, "limit": limit}


@router.get("/topics/{topic_id}", response_model=TopicDetail)
def get_topic(topic_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    return _load_topic_detail(db, topic_id)


@router.post("/topics", response_model=TopicDetail, status_code=status.HTTP_201_CREATED)
def create_topic(body: TopicCreate, _admin: AdminDep, db: DbDep):
    import re as _re

    data = body.model_dump()
    # Auto-generate slug if not provided
    if not data.get("slug"):
        data["slug"] = _re.sub(r"[^a-z0-9]+", "-", body.title.lower()).strip("-")
    # Check slug uniqueness
    if db.scalar(select(Topic).where(Topic.slug == data["slug"])):
        raise HTTPException(status_code=409, detail="Topic with this slug already exists")
    if data.get("action_items") is None:
        data["action_items"] = []
    obj = Topic(**data)
    obj.search_text = " ".join(filter(None, [
        obj.title or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _load_topic_detail(db, obj.id)


@router.patch("/topics/{topic_id}", response_model=TopicDetail)
def update_topic(topic_id: uuid.UUID, body: TopicUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.search_text = " ".join(filter(None, [
        obj.title or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.delete("/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_topic(topic_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.delete(obj)
    db.commit()


@router.patch("/topics/{topic_id}/publish", response_model=TopicDetail)
def publish_topic(topic_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    obj.status = "published"
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.patch("/topics/{topic_id}/unpublish", response_model=TopicDetail)
def unpublish_topic(topic_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    obj.status = "draft"
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.post("/topics/{topic_id}/image", response_model=TopicDetail)
async def upload_topic_image(topic_id: uuid.UUID, file: UploadFile, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
    ext = ext_map.get(file.content_type, "bin")
    s3_key = f"assets/topics/{topic_id}/image.{ext}"

    data = await file.read()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=data,
        ContentType=file.content_type,
    )
    obj.image_url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.put("/topics/{topic_id}/faqs", response_model=TopicDetail)
def update_topic_faqs(topic_id: uuid.UUID, body: FaqsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.query(TopicFaq).filter_by(topic_id=topic_id).delete()
    for item in body.items:
        db.add(TopicFaq(topic_id=topic_id, faq_id=item.faq_id, sort_order=item.sort_order))
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.put("/topics/{topic_id}/resources", response_model=TopicDetail)
def update_topic_resources(topic_id: uuid.UUID, body: TopicResourcesUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.query(TopicResource).filter_by(topic_id=topic_id).delete()
    for item in body.items:
        db.add(TopicResource(topic_id=topic_id, content_asset_id=item.content_asset_id, sort_order=item.sort_order))
    db.commit()
    return _load_topic_detail(db, topic_id)


@router.get("/topics/public/slug/{slug}", response_model=TopicDetail)
def get_topic_by_slug_public(slug: str, db: DbDep):
    """Public — return a single topic by slug (published only)."""
    stmt = (
        select(Topic)
        .where(Topic.slug == slug, Topic.status == "published")
        .options(
            selectinload(Topic.goal),
            selectinload(Topic.faqs),
            selectinload(Topic.resources).selectinload(ContentAsset.asset_type),
        )
    )
    topic = db.scalar(stmt)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic


# ── Objectives ────────────────────────────────────────────────────────────────

@router.get("/objectives", response_model=list[ObjectiveOut])
def list_objectives(db: DbDep):
    return db.scalars(select(Objective).order_by(Objective.name)).all()


@router.get("/objectives/public", response_model=list[ObjectiveOut])
def list_objectives_public(db: DbDep):
    """Public endpoint — list all objectives."""
    return db.scalars(select(Objective).order_by(Objective.name)).all()


@router.post("/objectives", response_model=ObjectiveOut, status_code=status.HTTP_201_CREATED)
def create_objective(body: ObjectiveCreate, _admin: AdminDep, db: DbDep):
    obj = Objective(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/objectives/{objective_id}", response_model=ObjectiveOut)
def update_objective(objective_id: uuid.UUID, body: ObjectiveUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Objective, objective_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Objective not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/objectives/{objective_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_objective(objective_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Objective, objective_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Objective not found")
    db.delete(obj)
    db.commit()


# ── Content Assets ────────────────────────────────────────────────────────────

def _load_asset_detail(db: DbDep, asset_id: uuid.UUID) -> ContentAsset:
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.id == asset_id)
        .options(
            selectinload(ContentAsset.asset_type),
            selectinload(ContentAsset.objectives),
            selectinload(ContentAsset.goals),
            selectinload(ContentAsset.topics).selectinload(Topic.goal),
            selectinload(ContentAsset.workshops),
            selectinload(ContentAsset.cohorts),
            selectinload(ContentAsset.faqs),
            selectinload(ContentAsset.resources).selectinload(ContentAsset.asset_type),
        )
    )
    obj = db.scalar(stmt)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    return obj


def _resolve_resources(db: DbDep, asset: ContentAsset) -> list[ContentAsset]:
    """Return hand-picked resources or auto-fallback by shared objectives/goals."""
    if asset.resources:
        return list(asset.resources)

    obj_ids = [o.id for o in asset.objectives]
    goal_ids = [g.id for g in asset.goals]
    if not obj_ids and not goal_ids:
        return []

    from src.content.models import ContentAssetObjective as CAO, ContentAssetGoal as CAG

    subq_obj = select(CAO.content_asset_id).where(CAO.objective_id.in_(obj_ids))
    subq_goal = select(CAG.content_asset_id).where(CAG.goal_id.in_(goal_ids))
    stmt = (
        select(ContentAsset)
        .options(selectinload(ContentAsset.asset_type))
        .where(
            ContentAsset.status == "published",
            ContentAsset.id != asset.id,
            or_(
                ContentAsset.id.in_(subq_obj),
                ContentAsset.id.in_(subq_goal),
            ),
        )
        .limit(6)
    )
    return list(db.scalars(stmt).all())


@router.get("/assets", response_model=ContentAssetListResponse)
def list_assets(
    db: DbDep,
    search: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    asset_type_id: Annotated[uuid.UUID | None, Query()] = None,
    objective_id: Annotated[uuid.UUID | None, Query()] = None,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    topic_id: Annotated[uuid.UUID | None, Query()] = None,
    cohort_id: Annotated[uuid.UUID | None, Query()] = None,
    is_featured: Annotated[bool | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_dir: Annotated[str, Query()] = "desc",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    stmt = select(ContentAsset).options(selectinload(ContentAsset.asset_type))

    if search:
        stmt = stmt.where(ContentAsset.name.ilike(f"%{search}%"))
    if status:
        stmt = stmt.where(ContentAsset.status == status)
    if asset_type_id:
        stmt = stmt.where(ContentAsset.asset_type_id == asset_type_id)
    if is_featured is not None:
        stmt = stmt.where(ContentAsset.is_featured == is_featured)
    if objective_id:
        stmt = stmt.join(ContentAssetObjective).where(ContentAssetObjective.objective_id == objective_id)
    if goal_id:
        stmt = stmt.join(ContentAssetGoal).where(ContentAssetGoal.goal_id == goal_id)
    if topic_id:
        stmt = stmt.join(TopicResource, TopicResource.content_asset_id == ContentAsset.id).where(TopicResource.topic_id == topic_id)
    if cohort_id:
        stmt = stmt.join(ContentAssetCohort).where(ContentAssetCohort.cohort_id == cohort_id)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.scalar(count_stmt)

    sort_col = getattr(ContentAsset, sort_by, ContentAsset.created_at)
    if sort_dir == "desc":
        stmt = stmt.order_by(sort_col.desc())
    else:
        stmt = stmt.order_by(sort_col.asc())

    items = db.scalars(stmt.offset(skip).limit(limit)).all()
    return {"items": items, "total": total, "skip": skip, "limit": limit}


def _parse_csv_uuids(value: str | None) -> list[uuid.UUID]:
    """Parse a comma-separated string of UUIDs into a list."""
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    try:
        return [uuid.UUID(p) for p in parts]
    except ValueError:
        return []


@router.get("/assets/public", response_model=ContentAssetListResponse)
def list_assets_public(
    db: DbDep,
    search: Annotated[str | None, Query()] = None,
    asset_type_id: Annotated[uuid.UUID | None, Query()] = None,
    asset_type_ids: Annotated[str | None, Query()] = None,
    objective_id: Annotated[uuid.UUID | None, Query()] = None,
    objective_ids: Annotated[str | None, Query()] = None,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    goal_ids: Annotated[str | None, Query()] = None,
    topic_id: Annotated[uuid.UUID | None, Query()] = None,
    topic_ids: Annotated[str | None, Query()] = None,
    cohort_id: Annotated[uuid.UUID | None, Query()] = None,
    is_featured: Annotated[bool | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_dir: Annotated[str, Query()] = "desc",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Public endpoint — only returns published assets."""
    _NAME_THRESHOLD = 0.3
    _DESC_THRESHOLD = 0.4

    stmt = (
        select(ContentAsset)
        .options(selectinload(ContentAsset.asset_type))
        .where(ContentAsset.status == "published")
    )

    has_search = bool(search and search.strip())
    if has_search:
        search = search.strip()
        name_sim = func.word_similarity(literal(search), ContentAsset.name)
        desc_sim = func.word_similarity(
            literal(search),
            func.coalesce(ContentAsset.description, literal("")),
        )
        stmt = stmt.where(
            or_(
                name_sim > _NAME_THRESHOLD,
                desc_sim > _DESC_THRESHOLD,
                ContentAsset.name.ilike(f"%{search}%"),
            )
        )

    # Asset type filtering (single or multi)
    at_ids = _parse_csv_uuids(asset_type_ids)
    if at_ids:
        stmt = stmt.where(ContentAsset.asset_type_id.in_(at_ids))
    elif asset_type_id:
        stmt = stmt.where(ContentAsset.asset_type_id == asset_type_id)

    if is_featured is not None:
        stmt = stmt.where(ContentAsset.is_featured == is_featured)

    # Objective filtering (single or multi)
    obj_ids = _parse_csv_uuids(objective_ids)
    if obj_ids:
        stmt = stmt.join(ContentAssetObjective).where(ContentAssetObjective.objective_id.in_(obj_ids))
    elif objective_id:
        stmt = stmt.join(ContentAssetObjective).where(ContentAssetObjective.objective_id == objective_id)

    # Goal filtering (single or multi)
    g_ids = _parse_csv_uuids(goal_ids)
    if g_ids:
        stmt = stmt.join(ContentAssetGoal).where(ContentAssetGoal.goal_id.in_(g_ids))
    elif goal_id:
        stmt = stmt.join(ContentAssetGoal).where(ContentAssetGoal.goal_id == goal_id)

    # Topic filtering (single or multi)
    t_ids = _parse_csv_uuids(topic_ids)
    if t_ids:
        stmt = stmt.join(TopicResource, TopicResource.content_asset_id == ContentAsset.id).where(TopicResource.topic_id.in_(t_ids))
    elif topic_id:
        stmt = stmt.join(TopicResource, TopicResource.content_asset_id == ContentAsset.id).where(TopicResource.topic_id == topic_id)

    if cohort_id:
        stmt = stmt.join(ContentAssetCohort).where(ContentAssetCohort.cohort_id == cohort_id)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.scalar(count_stmt)

    if has_search:
        name_sim = func.word_similarity(literal(search), ContentAsset.name)
        desc_sim = func.word_similarity(
            literal(search),
            func.coalesce(ContentAsset.description, literal("")),
        )
        relevance = name_sim * literal(2) + desc_sim
        stmt = stmt.order_by(relevance.desc(), ContentAsset.created_at.desc())
    else:
        sort_col = getattr(ContentAsset, sort_by, ContentAsset.created_at)
        if sort_dir == "desc":
            stmt = stmt.order_by(sort_col.desc())
        else:
            stmt = stmt.order_by(sort_col.asc())

    items = db.scalars(stmt.offset(skip).limit(limit)).all()
    return {"items": items, "total": total, "skip": skip, "limit": limit}



@router.get("/assets/{asset_id}/public", response_model=ContentAssetDetail)
def get_asset_public(asset_id: uuid.UUID, db: DbDep):
    """Public endpoint — only returns published assets."""
    asset = _load_asset_detail(db, asset_id)
    if asset.status != "published":
        raise HTTPException(status_code=404, detail="Content asset not found")
    asset.resources = _resolve_resources(db, asset)
    return asset


@router.get("/assets/{asset_id}", response_model=ContentAssetDetail)
def get_asset(asset_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    asset = _load_asset_detail(db, asset_id)
    asset.resources = _resolve_resources(db, asset)
    return asset


@router.post("/assets", response_model=ContentAssetDetail, status_code=status.HTTP_201_CREATED)
def create_asset(_admin: AdminDep, body: ContentAssetCreate, db: DbDep):
    obj = ContentAsset(**body.model_dump())
    obj.search_text = " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))
    obj.read_time_minutes = _calculate_read_time(obj.content, obj.summary)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _load_asset_detail(db, obj.id)


@router.patch("/assets/{asset_id}", response_model=ContentAssetDetail)
def update_asset(asset_id: uuid.UUID, body: ContentAssetUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    obj.search_text = " ".join(filter(None, [
        obj.name or "",
        obj.description or "",
        extract_text(obj.summary),
        extract_text(obj.content),
    ]))
    obj.read_time_minutes = _calculate_read_time(obj.content, obj.summary)
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.delete(obj)
    db.commit()


@router.patch("/assets/{asset_id}/publish", response_model=ContentAssetDetail)
def publish_asset(asset_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    obj.status = "published"
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.patch("/assets/{asset_id}/unpublish", response_model=ContentAssetDetail)
def unpublish_asset(asset_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    obj.status = "draft"
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.post("/assets/{asset_id}/image", response_model=ContentAssetDetail)
async def upload_asset_image(asset_id: uuid.UUID, file: UploadFile, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
    ext = ext_map.get(file.content_type, "bin")
    s3_key = f"assets/content/{asset_id}/image.{ext}"

    data = await file.read()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    s3.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=data,
        ContentType=file.content_type,
    )
    obj.image_url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    db.commit()
    return _load_asset_detail(db, asset_id)


# ── Relationship management ───────────────────────────────────────────────────

@router.put("/assets/{asset_id}/objectives", response_model=ContentAssetDetail)
def update_asset_objectives(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetObjective).filter_by(content_asset_id=asset_id).delete()
    for oid in body.ids:
        db.add(ContentAssetObjective(content_asset_id=asset_id, objective_id=oid))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/goals", response_model=ContentAssetDetail)
def update_asset_goals(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetGoal).filter_by(content_asset_id=asset_id).delete()
    for gid in body.ids:
        db.add(ContentAssetGoal(content_asset_id=asset_id, goal_id=gid))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/workshops", response_model=ContentAssetDetail)
def update_asset_workshops(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(WorkshopResource).filter_by(content_asset_id=asset_id).delete()
    for wid in body.ids:
        db.add(WorkshopResource(content_asset_id=asset_id, workshop_id=wid))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/cohorts", response_model=ContentAssetDetail)
def update_asset_cohorts(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetCohort).filter_by(content_asset_id=asset_id).delete()
    for cid in body.ids:
        db.add(ContentAssetCohort(content_asset_id=asset_id, cohort_id=cid))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/faqs", response_model=ContentAssetDetail)
def update_asset_faqs(asset_id: uuid.UUID, body: FaqsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetFaq).filter_by(content_asset_id=asset_id).delete()
    for item in body.items:
        db.add(ContentAssetFaq(content_asset_id=asset_id, faq_id=item.faq_id, sort_order=item.sort_order))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/resources", response_model=ContentAssetDetail)
def update_asset_resources(asset_id: uuid.UUID, body: ResourcesUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetResource).filter_by(content_asset_id=asset_id).delete()
    for item in body.items:
        db.add(ContentAssetResource(content_asset_id=asset_id, resource_id=item.resource_id, sort_order=item.sort_order))
    db.commit()
    asset = _load_asset_detail(db, asset_id)
    asset.resources = _resolve_resources(db, asset)
    return asset


# ── FAQs ──────────────────────────────────────────────────────────────────────

@router.get("/faqs", response_model=list[FaqOut])
def list_faqs(_admin: AdminDep, db: DbDep):
    return db.scalars(select(Faq).order_by(Faq.created_at.desc())).all()


@router.post("/faqs", response_model=FaqOut, status_code=status.HTTP_201_CREATED)
def create_faq(body: FaqCreate, _admin: AdminDep, db: DbDep):
    obj = Faq(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/faqs/{faq_id}", response_model=FaqOut)
def update_faq(faq_id: uuid.UUID, body: FaqUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Faq, faq_id)
    if not obj:
        raise HTTPException(status_code=404, detail="FAQ not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/faqs/{faq_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_faq(faq_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Faq, faq_id)
    if not obj:
        raise HTTPException(status_code=404, detail="FAQ not found")
    db.delete(obj)
    db.commit()


# ── Reader Questions ───────────────────────────────────────────────────────────

@router.post("/assets/{asset_id}/questions", response_model=ReaderQuestionOut, status_code=status.HTTP_201_CREATED)
def submit_question(asset_id: uuid.UUID, body: ReaderQuestionCreate, db: DbDep):
    """Public — no auth required."""
    if not db.get(ContentAsset, asset_id):
        raise HTTPException(status_code=404, detail="Content asset not found")
    obj = ReaderQuestion(content_asset_id=asset_id, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/assets/{asset_id}/questions", response_model=list[ReaderQuestionOut])
def list_questions(asset_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    if not db.get(ContentAsset, asset_id):
        raise HTTPException(status_code=404, detail="Content asset not found")
    return db.scalars(
        select(ReaderQuestion)
        .where(ReaderQuestion.content_asset_id == asset_id)
        .order_by(ReaderQuestion.created_at.desc())
    ).all()


@router.patch("/questions/{question_id}", response_model=ReaderQuestionOut)
def update_question_status(
    question_id: uuid.UUID,
    status: Annotated[str, Query()],
    _admin: AdminDep,
    db: DbDep,
):
    obj = db.get(ReaderQuestion, question_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Question not found")
    if status not in ("pending", "answered", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be pending, answered, or dismissed")
    obj.status = status
    db.commit()
    db.refresh(obj)
    return obj


# ── Grade Sets ───────────────────────────────────────────────────────────────


def _get_default_grade_set_id(db) -> uuid.UUID | None:
    """Return the ID of the default grade set."""
    gs = db.query(GradeSet).filter(GradeSet.is_default.is_(True)).first()
    return gs.id if gs else None


@router.get("/grade-sets", response_model=list[GradeSetSummary])
def list_grade_sets(_admin: AdminDep, db: DbDep):
    """Admin: list all grade sets."""
    return db.scalars(select(GradeSet).order_by(GradeSet.name)).all()


@router.post("/grade-sets", response_model=GradeSetSummary, status_code=201)
def create_grade_set(body: GradeSetCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a new grade set."""
    obj = GradeSet(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/grade-sets/{grade_set_id}", response_model=GradeSetSummary)
def update_grade_set(grade_set_id: uuid.UUID, body: GradeSetUpdate, _admin: AdminDep, db: DbDep):
    """Admin: update a grade set."""
    obj = db.get(GradeSet, grade_set_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Grade set not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return obj


@router.delete("/grade-sets/{grade_set_id}", status_code=204)
def delete_grade_set(grade_set_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: delete a grade set (must have no configs)."""
    obj = db.get(GradeSet, grade_set_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Grade set not found")
    if obj.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default grade set")
    config_count = db.query(GradeConfig).filter(GradeConfig.grade_set_id == grade_set_id).count()
    if config_count > 0:
        raise HTTPException(status_code=409, detail="Grade set has configs assigned. Remove them first.")
    db.delete(obj)
    db.commit()


# ── Grade Configs ────────────────────────────────────────────────────────────


def _gc_summary(gc: GradeConfig) -> GradeConfigSummary:
    """Build a lightweight GradeConfigSummary from a loaded GradeConfig."""
    return GradeConfigSummary(
        id=gc.id,
        grade_set_id=gc.grade_set_id,
        grade=gc.grade,
        label=gc.label,
        description=gc.description,
        video_overview_url=gc.video_overview_url,
        icon=gc.icon,
        bg_color=gc.bg_color,
        page_title=gc.page_title,
        page_description=gc.page_description,
        banner_image_url=gc.banner_image_url,
        sort_order=gc.sort_order,
        goal_ids=[g.id for g in gc.goals],
    )


def _load_grade_config(db, gc: GradeConfig) -> GradeConfigOut:
    """Build a GradeConfigOut with goals and their published topics/assets."""
    goals_with_topics = []
    for goal in gc.goals:
        published_assets = [
            a for a in goal.content_assets if a.status == "published"
        ]
        published_topics = [
            t for t in goal.topics if t.status == "published"
        ]
        goals_with_topics.append(
            GoalWithTopics(
                id=goal.id,
                name=goal.name,
                description=goal.description,
                icon_url=goal.icon_url,
                slug=goal.slug,
                suggested_grades=goal.suggested_grades,
                sort_order=goal.sort_order,
                topics=[TopicSummary.model_validate(t) for t in published_topics],
                content_assets=[ContentAssetSummary.model_validate(a) for a in published_assets],
            )
        )
    return GradeConfigOut(
        id=gc.id,
        grade_set_id=gc.grade_set_id,
        grade=gc.grade,
        label=gc.label,
        description=gc.description,
        video_overview_url=gc.video_overview_url,
        icon=gc.icon,
        bg_color=gc.bg_color,
        page_title=gc.page_title,
        page_description=gc.page_description,
        banner_image_url=gc.banner_image_url,
        sort_order=gc.sort_order,
        goals=goals_with_topics,
        created_at=gc.created_at,
    )


@router.get("/grade-configs/public", response_model=list[GradeConfigOut])
def list_grade_configs_public(
    db: DbDep,
    school_slug: str | None = Query(default=None),
):
    """Public: list grade configs with goals and published assets/topics.

    If school_slug is provided, returns the grade set assigned to that school.
    Otherwise falls back to the default grade set.
    """
    grade_set_id: uuid.UUID | None = None

    if school_slug:
        school = db.query(School).filter(School.slug == school_slug).first()
        if school and school.grade_set_id:
            grade_set_id = school.grade_set_id

    if grade_set_id is None:
        grade_set_id = _get_default_grade_set_id(db)

    stmt = (
        select(GradeConfig)
        .where(GradeConfig.grade_set_id == grade_set_id)
        .outerjoin(GradeConfigGoal, GradeConfig.id == GradeConfigGoal.grade_config_id)
        .outerjoin(Goal, GradeConfigGoal.goal_id == Goal.id)
        .options(
            contains_eager(GradeConfig.goals)
            .selectinload(Goal.content_assets)
            .selectinload(ContentAsset.asset_type),
            contains_eager(GradeConfig.goals)
            .selectinload(Goal.topics),
        )
        .order_by(GradeConfig.grade, GradeConfigGoal.sort_order)
    )
    configs = db.execute(stmt).unique().scalars().all()
    return [_load_grade_config(db, gc) for gc in configs]


@router.get("/grade-configs/public/{grade}", response_model=GradeConfigOut)
def get_grade_config_by_grade(
    grade: int,
    db: DbDep,
    school_slug: str | None = Query(default=None),
):
    """Public: get a grade config by its grade number."""
    grade_set_id: uuid.UUID | None = None

    if school_slug:
        school = db.query(School).filter(School.slug == school_slug).first()
        if school and school.grade_set_id:
            grade_set_id = school.grade_set_id

    if grade_set_id is None:
        grade_set_id = _get_default_grade_set_id(db)

    gc = (
        db.query(GradeConfig)
        .filter(GradeConfig.grade_set_id == grade_set_id, GradeConfig.grade == grade)
        .one_or_none()
    )
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")
    return _load_grade_config(db, gc)


@router.get("/grade-configs", response_model=list[GradeConfigSummary])
def list_grade_configs(
    _admin: AdminDep,
    db: DbDep,
    grade_set_id: uuid.UUID | None = Query(default=None),
):
    """Admin: list grade configs (lightweight, with goal IDs only).

    Optionally filter by grade_set_id.
    """
    q = db.query(GradeConfig).options(selectinload(GradeConfig.goals))
    if grade_set_id:
        q = q.filter(GradeConfig.grade_set_id == grade_set_id)
    configs = q.order_by(GradeConfig.grade).all()
    return [_gc_summary(gc) for gc in configs]


@router.post("/grade-configs", response_model=GradeConfigSummary, status_code=201)
def create_grade_config(body: GradeConfigCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a new grade config within a grade set."""
    # Verify grade set exists
    gs = db.get(GradeSet, body.grade_set_id)
    if not gs:
        raise HTTPException(status_code=404, detail="Grade set not found")

    existing = (
        db.query(GradeConfig)
        .filter(GradeConfig.grade_set_id == body.grade_set_id, GradeConfig.grade == body.grade)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"Grade {body.grade} already exists in this grade set")
    gc = GradeConfig(**body.model_dump())
    db.add(gc)
    db.commit()
    db.refresh(gc)
    return GradeConfigSummary(
        id=gc.id, grade_set_id=gc.grade_set_id, grade=gc.grade, label=gc.label,
        description=gc.description, video_overview_url=gc.video_overview_url,
        icon=gc.icon, bg_color=gc.bg_color, page_title=gc.page_title,
        page_description=gc.page_description, banner_image_url=gc.banner_image_url,
        sort_order=gc.sort_order, goal_ids=[],
    )


@router.patch("/grade-configs/{grade_config_id}", response_model=GradeConfigSummary)
def update_grade_config(
    grade_config_id: uuid.UUID, body: GradeConfigUpdate, _admin: AdminDep, db: DbDep
):
    """Admin: update a grade config's fields."""
    gc = db.get(GradeConfig, grade_config_id)
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(gc, field, value)
    db.commit()
    db.refresh(gc)
    gc = db.query(GradeConfig).options(selectinload(GradeConfig.goals)).filter(GradeConfig.id == gc.id).one()
    return _gc_summary(gc)


@router.delete("/grade-configs/{grade_config_id}", status_code=204)
def delete_grade_config(grade_config_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: delete a grade config."""
    gc = db.get(GradeConfig, grade_config_id)
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")
    db.delete(gc)
    db.commit()


@router.put("/grade-configs/{grade_config_id}/goals", response_model=GradeConfigSummary)
def update_grade_config_goals(
    grade_config_id: uuid.UUID, body: GradeConfigGoalsUpdate, _admin: AdminDep, db: DbDep
):
    """Admin: replace the goals assigned to a grade config. Only top-level goals allowed."""
    gc = db.get(GradeConfig, grade_config_id)
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")

    # Clear existing
    db.query(GradeConfigGoal).filter(GradeConfigGoal.grade_config_id == grade_config_id).delete()

    # Insert new with sort order
    for i, goal_id in enumerate(body.goal_ids):
        db.add(GradeConfigGoal(grade_config_id=grade_config_id, goal_id=goal_id, sort_order=i))
    db.commit()

    gc = db.query(GradeConfig).options(selectinload(GradeConfig.goals)).filter(GradeConfig.id == gc.id).one()
    return _gc_summary(gc)
