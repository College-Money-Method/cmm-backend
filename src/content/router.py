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
    ContentAssetObjective,
    ContentAssetResource,
    WorkshopResource,
    Faq,
    Goal,
    GradeConfig,
    GradeConfigGoal,
    GradeSet,
    Milestone,
    MilestoneGoal,
    MilestoneGradeConfig,
    MilestoneTopic,
    MilestoneWorkshop,
    Objective,
    ReaderQuestion,
    ResourceCategory,
    ResourceCategoryTopic,
    ResourceCategoryWorkshop,
    Topic,
    TopicFaq,
    TopicResource,
)
from src.schools.models import School
from src.workshops.models import Workshop
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
    MilestoneCreate,
    MilestoneDetail,
    MilestoneOut,
    MilestoneUpdate,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    ReaderQuestionCreate,
    ReaderQuestionOut,
    RelationshipsUpdate,
    ResourceCategoryCreate,
    ResourceCategoryDetail,
    ResourceCategoryOut,
    ResourceCategoryUpdate,
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
from src.storage.models import StorageFile
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


@router.get("/topics/public", response_model=list[TopicListItem])
def list_topics_public(db: DbDep):
    """Public — list all published topics ordered by sort_order, title."""
    stmt = (
        select(Topic)
        .where(Topic.status == "published")
        .options(selectinload(Topic.goal))
        .order_by(Topic.sort_order.asc(), Topic.title.asc())
    )
    return db.scalars(stmt).all()


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
    topic.resources = [r for r in topic.resources if r.status == "published"]
    return topic


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
        return [r for r in asset.resources if r.status == "published"]

    obj_ids = [o.id for o in asset.objectives]
    topic_ids = [t.id for t in asset.topics]
    if not obj_ids and not topic_ids:
        return []

    from src.content.models import ContentAssetObjective as CAO

    subq_obj = select(CAO.content_asset_id).where(CAO.objective_id.in_(obj_ids))
    subq_topic = select(TopicResource.content_asset_id).where(TopicResource.topic_id.in_(topic_ids))
    stmt = (
        select(ContentAsset)
        .options(selectinload(ContentAsset.asset_type))
        .where(
            ContentAsset.status == "published",
            ContentAsset.id != asset.id,
            or_(
                ContentAsset.id.in_(subq_obj),
                ContentAsset.id.in_(subq_topic),
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
        goal_topic_subq = select(Topic.id).where(Topic.goal_id == goal_id)
        stmt = stmt.where(ContentAsset.id.in_(
            select(TopicResource.content_asset_id).where(TopicResource.topic_id.in_(goal_topic_subq))
        ))
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


# Ranking formula weights — admin boost has ~3.0 weight so 1 boost ≈ 20 clicks.
_POPULARITY_BOOST_WEIGHT = 3.0


def _parse_csv_ints(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        return [int(p.strip()) for p in value.split(",") if p.strip()]
    except ValueError:
        return []


def _parse_csv_strings(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


@router.get("/assets/public", response_model=ContentAssetListResponse)
def list_assets_public(
    db: DbDep,
    search: Annotated[str | None, Query()] = None,
    asset_type_id: Annotated[uuid.UUID | None, Query()] = None,
    asset_type_ids: Annotated[str | None, Query()] = None,
    asset_buckets: Annotated[str | None, Query()] = None,
    objective_id: Annotated[uuid.UUID | None, Query()] = None,
    objective_ids: Annotated[str | None, Query()] = None,
    goal_id: Annotated[uuid.UUID | None, Query()] = None,
    goal_ids: Annotated[str | None, Query()] = None,
    topic_id: Annotated[uuid.UUID | None, Query()] = None,
    topic_ids: Annotated[str | None, Query()] = None,
    category_ids: Annotated[str | None, Query()] = None,
    milestone_ids: Annotated[str | None, Query()] = None,
    grades: Annotated[str | None, Query()] = None,
    cohort_id: Annotated[uuid.UUID | None, Query()] = None,
    school_id: Annotated[uuid.UUID | None, Query()] = None,
    is_featured: Annotated[bool | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_dir: Annotated[str, Query()] = "desc",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
):
    """Public endpoint — only returns published assets with public asset types."""
    _NAME_THRESHOLD = 0.3
    _DESC_THRESHOLD = 0.4

    # Base query: published assets with public asset types. LEFT JOIN asset_types
    # so assets with no asset_type (shouldn't happen but safe) still appear.
    stmt = (
        select(ContentAsset)
        .options(selectinload(ContentAsset.asset_type))
        .outerjoin(AssetType, ContentAsset.asset_type_id == AssetType.id)
        .where(ContentAsset.status == "published")
        .where(or_(AssetType.id.is_(None), AssetType.is_public.is_(True)))
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

    # Asset bucket filtering (tools / video / guide)
    # "tools" resolves to all asset types with is_tool=True;
    # "video" and "guide" match display_bucket directly.
    buckets = _parse_csv_strings(asset_buckets)
    if buckets:
        bucket_conditions = []
        non_tool_buckets = [b for b in buckets if b != "tools"]
        if "tools" in buckets:
            bucket_conditions.append(AssetType.is_tool.is_(True))
        if non_tool_buckets:
            bucket_conditions.append(AssetType.display_bucket.in_(non_tool_buckets))
        if bucket_conditions:
            stmt = stmt.where(or_(*bucket_conditions))

    if is_featured is not None:
        stmt = stmt.where(ContentAsset.is_featured == is_featured)

    # Objective filtering (single or multi)
    obj_ids = _parse_csv_uuids(objective_ids)
    if obj_ids:
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(ContentAssetObjective.content_asset_id).where(
                    ContentAssetObjective.objective_id.in_(obj_ids)
                )
            )
        )
    elif objective_id:
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(ContentAssetObjective.content_asset_id).where(
                    ContentAssetObjective.objective_id == objective_id
                )
            )
        )

    # Goal filtering: goal → topics → topic_resources → assets
    g_ids = _parse_csv_uuids(goal_ids)
    if g_ids:
        goal_topic_subq = select(Topic.id).where(Topic.goal_id.in_(g_ids))
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(TopicResource.content_asset_id).where(TopicResource.topic_id.in_(goal_topic_subq))
            )
        )
    elif goal_id:
        goal_topic_subq = select(Topic.id).where(Topic.goal_id == goal_id)
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(TopicResource.content_asset_id).where(TopicResource.topic_id.in_(goal_topic_subq))
            )
        )

    # Topic filtering (single or multi)
    t_ids = _parse_csv_uuids(topic_ids)
    if t_ids:
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(TopicResource.content_asset_id).where(
                    TopicResource.topic_id.in_(t_ids)
                )
            )
        )
    elif topic_id:
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(TopicResource.content_asset_id).where(
                    TopicResource.topic_id == topic_id
                )
            )
        )

    # Category filtering: category → (topics ∪ workshops) → assets
    cat_ids = _parse_csv_uuids(category_ids)
    if cat_ids:
        cat_topic_subq = select(ResourceCategoryTopic.topic_id).where(
            ResourceCategoryTopic.resource_category_id.in_(cat_ids)
        )
        cat_workshop_subq = select(ResourceCategoryWorkshop.workshop_id).where(
            ResourceCategoryWorkshop.resource_category_id.in_(cat_ids)
        )
        cat_asset_subq = select(TopicResource.content_asset_id).where(
            TopicResource.topic_id.in_(cat_topic_subq)
        ).union(
            select(WorkshopResource.content_asset_id).where(
                WorkshopResource.workshop_id.in_(cat_workshop_subq)
            )
        )
        stmt = stmt.where(ContentAsset.id.in_(cat_asset_subq))

    # Grade filtering: grade_configs(grade) → goals → topics → topic_resources
    #                  + workshops(suggested_grades contains grade) → workshop_resources
    grade_ints = _parse_csv_ints(grades)
    if grade_ints:
        goal_id_subq = (
            select(GradeConfigGoal.goal_id)
            .join(GradeConfig, GradeConfig.id == GradeConfigGoal.grade_config_id)
            .where(GradeConfig.grade.in_(grade_ints))
        )
        topic_id_subq = select(Topic.id).where(Topic.goal_id.in_(goal_id_subq))
        topic_asset_subq = (
            select(TopicResource.content_asset_id)
            .where(TopicResource.topic_id.in_(topic_id_subq))
        )
        workshop_grade_conditions = [
            Workshop.suggested_grades.op("~")(f"(^|,){g}(,|$)")
            for g in grade_ints
        ]
        workshop_asset_subq = (
            select(WorkshopResource.content_asset_id)
            .join(Workshop, Workshop.id == WorkshopResource.workshop_id)
            .where(or_(*workshop_grade_conditions))
        )
        grade_asset_combined = topic_asset_subq.union(workshop_asset_subq)
        stmt = stmt.where(ContentAsset.id.in_(grade_asset_combined))

    # Milestone filtering: milestone → (goals→topics ∪ direct topics ∪ workshops) → assets
    ms_ids = _parse_csv_uuids(milestone_ids)
    if ms_ids:
        ms_goal_subq = select(MilestoneGoal.goal_id).where(MilestoneGoal.milestone_id.in_(ms_ids))
        ms_topics_from_goals_subq = select(Topic.id).where(Topic.goal_id.in_(ms_goal_subq))
        ms_direct_topic_subq = select(MilestoneTopic.topic_id).where(MilestoneTopic.milestone_id.in_(ms_ids))
        ms_all_topics_subq = ms_topics_from_goals_subq.union(ms_direct_topic_subq)
        ms_workshop_subq = select(MilestoneWorkshop.workshop_id).where(
            MilestoneWorkshop.milestone_id.in_(ms_ids)
        )
        ms_asset_subq = (
            select(TopicResource.content_asset_id).where(
                TopicResource.topic_id.in_(ms_all_topics_subq)
            )
            .union(
                select(WorkshopResource.content_asset_id).where(
                    WorkshopResource.workshop_id.in_(ms_workshop_subq)
                )
            )
        )
        stmt = stmt.where(ContentAsset.id.in_(ms_asset_subq))

    if school_id:
        # Return assets accessible to this school:
        # published AND (attached to school's cohort OR attached to no cohort at all)
        school = db.get(School, school_id)
        if school and school.cohort_id:
            has_cohort_subq = select(ContentAssetCohort.content_asset_id)
            stmt = stmt.where(
                or_(
                    ~ContentAsset.id.in_(has_cohort_subq),
                    ContentAsset.id.in_(
                        select(ContentAssetCohort.content_asset_id).where(
                            ContentAssetCohort.cohort_id == school.cohort_id
                        )
                    ),
                )
            )
        # If school has no cohort_id, all published assets are accessible (no extra filter)
    elif cohort_id:
        stmt = stmt.where(
            ContentAsset.id.in_(
                select(ContentAssetCohort.content_asset_id).where(
                    ContentAssetCohort.cohort_id == cohort_id
                )
            )
        )

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
    elif sort_by == "popularity":
        # score = popularity_score * w + ln(1 + click_count)
        ranking = (
            func.coalesce(ContentAsset.popularity_score, literal(0))
            * literal(_POPULARITY_BOOST_WEIGHT)
            + func.ln(literal(1) + ContentAsset.click_count)
        )
        stmt = stmt.order_by(ranking.desc(), ContentAsset.created_at.desc())
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


@router.post("/assets/{asset_id}/file", response_model=ContentAssetDetail)
async def upload_asset_file(asset_id: uuid.UUID, file: UploadFile, _admin: AdminDep, db: DbDep):
    """Admin — upload any file to S3 at resources/{asset_id}/{filename}, stored as content_assets.link."""
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")

    filename = file.filename or "file"
    s3_key = f"resources/{asset_id}/{filename}"
    mime_type = file.content_type or "application/octet-stream"
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else None

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
        ContentType=mime_type,
    )
    s3_url = f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"
    obj.link = s3_url

    # Upsert storage_files registry (same s3_key = re-upload of this asset)
    existing = db.execute(select(StorageFile).where(StorageFile.s3_key == s3_key)).scalar_one_or_none()
    if existing:
        existing.s3_url = s3_url
        existing.original_filename = filename
        existing.extension = extension
        existing.mime_type = mime_type
        existing.file_size_bytes = len(data)
    else:
        db.add(StorageFile(
            s3_key=s3_key,
            s3_url=s3_url,
            original_filename=filename,
            extension=extension,
            mime_type=mime_type,
            file_size_bytes=len(data),
        ))

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
    """Admin: delete a grade set.

    Schools assigned to this grade set are automatically moved to the default
    grade set. All grade configs in the set are deleted first.
    """
    obj = db.get(GradeSet, grade_set_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Grade set not found")
    if obj.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default grade set")

    # Move all schools on this grade set to the default grade set
    default_gs = db.query(GradeSet).filter(GradeSet.is_default.is_(True)).first()
    default_id = default_gs.id if default_gs else None
    db.query(School).filter(School.grade_set_id == grade_set_id).update(
        {School.grade_set_id: default_id}, synchronize_session=False
    )

    # Delete all grade configs belonging to this set (FK is RESTRICT, must go first)
    db.query(GradeConfig).filter(GradeConfig.grade_set_id == grade_set_id).delete(
        synchronize_session=False
    )

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


# ── Click tracking ───────────────────────────────────────────────────────────

@router.post("/assets/{asset_id}/click", status_code=status.HTTP_204_NO_CONTENT)
def track_asset_click(asset_id: uuid.UUID, db: DbDep):
    """Public — increment click_count for a content asset."""
    result = (
        db.query(ContentAsset)
        .filter(ContentAsset.id == asset_id, ContentAsset.status == "published")
        .update(
            {ContentAsset.click_count: ContentAsset.click_count + 1},
            synchronize_session=False,
        )
    )
    if not result:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.commit()


# ── Resource Categories ──────────────────────────────────────────────────────

def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _load_resource_category_detail(db: DbDep, cat_id: uuid.UUID) -> ResourceCategory:
    stmt = (
        select(ResourceCategory)
        .where(ResourceCategory.id == cat_id)
        .options(
            selectinload(ResourceCategory.topics),
            selectinload(ResourceCategory.workshops),
        )
    )
    obj = db.scalar(stmt)
    if not obj:
        raise HTTPException(status_code=404, detail="Resource category not found")
    return obj


@router.get("/resource-categories", response_model=list[ResourceCategoryOut])
def list_resource_categories(_admin: AdminDep, db: DbDep):
    return db.scalars(
        select(ResourceCategory).order_by(ResourceCategory.sort_order, ResourceCategory.name)
    ).all()


@router.get("/resource-categories/public", response_model=list[ResourceCategoryOut])
def list_resource_categories_public(db: DbDep):
    """Public — list published categories."""
    return db.scalars(
        select(ResourceCategory)
        .where(ResourceCategory.status == "published")
        .order_by(ResourceCategory.sort_order, ResourceCategory.name)
    ).all()


@router.get("/resource-categories/{cat_id}", response_model=ResourceCategoryDetail)
def get_resource_category(cat_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    return _load_resource_category_detail(db, cat_id)


@router.post("/resource-categories", response_model=ResourceCategoryDetail, status_code=status.HTTP_201_CREATED)
def create_resource_category(body: ResourceCategoryCreate, _admin: AdminDep, db: DbDep):
    if db.scalar(select(ResourceCategory).where(ResourceCategory.name == body.name)):
        raise HTTPException(status_code=409, detail="Resource category with this name already exists")
    data = body.model_dump()
    if not data.get("slug"):
        data["slug"] = _slugify(body.name)
    if db.scalar(select(ResourceCategory).where(ResourceCategory.slug == data["slug"])):
        raise HTTPException(status_code=409, detail="Resource category with this slug already exists")
    obj = ResourceCategory(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _load_resource_category_detail(db, obj.id)


@router.patch("/resource-categories/{cat_id}", response_model=ResourceCategoryDetail)
def update_resource_category(
    cat_id: uuid.UUID, body: ResourceCategoryUpdate, _admin: AdminDep, db: DbDep
):
    obj = db.get(ResourceCategory, cat_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Resource category not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    return _load_resource_category_detail(db, cat_id)


@router.delete("/resource-categories/{cat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resource_category(cat_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(ResourceCategory, cat_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Resource category not found")
    db.delete(obj)
    db.commit()


@router.put("/resource-categories/{cat_id}/topics", response_model=ResourceCategoryDetail)
def update_resource_category_topics(
    cat_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(ResourceCategory, cat_id):
        raise HTTPException(status_code=404, detail="Resource category not found")
    db.query(ResourceCategoryTopic).filter_by(resource_category_id=cat_id).delete()
    for tid in body.ids:
        db.add(ResourceCategoryTopic(resource_category_id=cat_id, topic_id=tid))
    db.commit()
    return _load_resource_category_detail(db, cat_id)


@router.put("/resource-categories/{cat_id}/workshops", response_model=ResourceCategoryDetail)
def update_resource_category_workshops(
    cat_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(ResourceCategory, cat_id):
        raise HTTPException(status_code=404, detail="Resource category not found")
    db.query(ResourceCategoryWorkshop).filter_by(resource_category_id=cat_id).delete()
    for wid in body.ids:
        db.add(ResourceCategoryWorkshop(resource_category_id=cat_id, workshop_id=wid))
    db.commit()
    return _load_resource_category_detail(db, cat_id)


# ── Milestones ───────────────────────────────────────────────────────────────

def _load_milestone_detail(db: DbDep, ms_id: uuid.UUID) -> Milestone:
    stmt = (
        select(Milestone)
        .where(Milestone.id == ms_id)
        .options(
            selectinload(Milestone.grade_configs),
            selectinload(Milestone.goals),
            selectinload(Milestone.topics),
            selectinload(Milestone.workshops),
        )
    )
    obj = db.scalar(stmt)
    if not obj:
        raise HTTPException(status_code=404, detail="Milestone not found")
    return obj


@router.get("/milestones", response_model=list[MilestoneOut])
def list_milestones(_admin: AdminDep, db: DbDep):
    return db.scalars(select(Milestone).order_by(Milestone.sort_order, Milestone.name)).all()


@router.get("/milestones/public", response_model=list[MilestoneOut])
def list_milestones_public(db: DbDep):
    return db.scalars(select(Milestone).order_by(Milestone.sort_order, Milestone.name)).all()


@router.get("/milestones/{ms_id}", response_model=MilestoneDetail)
def get_milestone(ms_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    return _load_milestone_detail(db, ms_id)


@router.post("/milestones", response_model=MilestoneDetail, status_code=status.HTTP_201_CREATED)
def create_milestone(body: MilestoneCreate, _admin: AdminDep, db: DbDep):
    if db.scalar(select(Milestone).where(Milestone.name == body.name)):
        raise HTTPException(status_code=409, detail="Milestone with this name already exists")
    data = body.model_dump()
    if not data.get("slug"):
        data["slug"] = _slugify(body.name)
    if db.scalar(select(Milestone).where(Milestone.slug == data["slug"])):
        raise HTTPException(status_code=409, detail="Milestone with this slug already exists")
    obj = Milestone(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _load_milestone_detail(db, obj.id)


@router.patch("/milestones/{ms_id}", response_model=MilestoneDetail)
def update_milestone(ms_id: uuid.UUID, body: MilestoneUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Milestone, ms_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Milestone not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    return _load_milestone_detail(db, ms_id)


@router.delete("/milestones/{ms_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_milestone(ms_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Milestone, ms_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Milestone not found")
    db.delete(obj)
    db.commit()


@router.put("/milestones/{ms_id}/grade-configs", response_model=MilestoneDetail)
def update_milestone_grade_configs(
    ms_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(Milestone, ms_id):
        raise HTTPException(status_code=404, detail="Milestone not found")
    db.query(MilestoneGradeConfig).filter_by(milestone_id=ms_id).delete()
    for gcid in body.ids:
        db.add(MilestoneGradeConfig(milestone_id=ms_id, grade_config_id=gcid))
    db.commit()
    return _load_milestone_detail(db, ms_id)


@router.put("/milestones/{ms_id}/goals", response_model=MilestoneDetail)
def update_milestone_goals(
    ms_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(Milestone, ms_id):
        raise HTTPException(status_code=404, detail="Milestone not found")
    db.query(MilestoneGoal).filter_by(milestone_id=ms_id).delete()
    for gid in body.ids:
        db.add(MilestoneGoal(milestone_id=ms_id, goal_id=gid))
    db.commit()
    return _load_milestone_detail(db, ms_id)


@router.put("/milestones/{ms_id}/topics", response_model=MilestoneDetail)
def update_milestone_topics(
    ms_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(Milestone, ms_id):
        raise HTTPException(status_code=404, detail="Milestone not found")
    db.query(MilestoneTopic).filter_by(milestone_id=ms_id).delete()
    for tid in body.ids:
        db.add(MilestoneTopic(milestone_id=ms_id, topic_id=tid))
    db.commit()
    return _load_milestone_detail(db, ms_id)


@router.put("/milestones/{ms_id}/workshops", response_model=MilestoneDetail)
def update_milestone_workshops(
    ms_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep
):
    if not db.get(Milestone, ms_id):
        raise HTTPException(status_code=404, detail="Milestone not found")
    db.query(MilestoneWorkshop).filter_by(milestone_id=ms_id).delete()
    for wid in body.ids:
        db.add(MilestoneWorkshop(milestone_id=ms_id, workshop_id=wid))
    db.commit()
    return _load_milestone_detail(db, ms_id)
