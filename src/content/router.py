"""Content management API router."""

from __future__ import annotations

import io
import uuid
from typing import Annotated

import boto3
import requests
from fastapi import APIRouter, HTTPException, Query, UploadFile, status
from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import selectinload

from src.auth.deps import AdminDep
from src.content.models import (
    AssetType,
    ContentAsset,
    ContentAssetCohort,
    ContentAssetFaq,
    ContentAssetObjective,
    ContentAssetResource,
    ContentAssetTopic,
    ContentAssetWorkshop,
    Faq,
    GradeConfig,
    GradeConfigTopic,
    Objective,
    ReaderQuestion,
    Topic,
)
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
    GradeConfigCreate,
    GradeConfigOut,
    GradeConfigSummary,
    GradeConfigTopicsUpdate,
    GradeConfigUpdate,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    ReaderQuestionCreate,
    ReaderQuestionOut,
    RelationshipsUpdate,
    ResourcesUpdate,
    TopicCreate,
    TopicOut,
    TopicUpdate,
    TopicWithAssets,
)
from src.config import settings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/content", tags=["content"])

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


# ── Topics ────────────────────────────────────────────────────────────────────

@router.get("/topics", response_model=list[TopicOut])
def list_topics(db: DbDep):
    """Admin: list all topics as a tree (top-level with children nested)."""
    topics = (
        db.query(Topic)
        .options(selectinload(Topic.children))
        .filter(Topic.parent_id.is_(None))
        .order_by(Topic.sort_order, Topic.name)
        .all()
    )
    return topics


@router.get("/topics/public", response_model=list[TopicOut])
def list_topics_public(db: DbDep):
    """Public endpoint — list all topics."""
    return db.scalars(select(Topic).order_by(Topic.sort_order, Topic.name)).all()


@router.get("/topics/public/grade/{grade}", response_model=list[TopicWithAssets])
def list_topics_by_grade(grade: int, db: DbDep):
    """Public — return topics for a grade with their published content assets."""
    import re as _re

    stmt = (
        select(Topic)
        .options(selectinload(Topic.content_assets).selectinload(ContentAsset.asset_type))
        .order_by(Topic.sort_order, Topic.name)
    )
    topics = db.scalars(stmt).all()

    grade_str = str(grade)
    result = []
    for topic in topics:
        grades = [g.strip() for g in (topic.suggested_grades or "").split(",") if g.strip()]
        if grade_str not in grades:
            continue
        # Filter to published assets only
        topic.content_assets = [a for a in topic.content_assets if a.status == "published"]
        result.append(topic)
    return result


@router.get("/topics/public/slug/{slug}", response_model=TopicWithAssets)
def get_topic_by_slug(slug: str, db: DbDep):
    """Public — return a single topic by slug with published content assets."""
    stmt = (
        select(Topic)
        .where(Topic.slug == slug)
        .options(selectinload(Topic.content_assets).selectinload(ContentAsset.asset_type))
    )
    topic = db.scalar(stmt)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    topic.content_assets = [a for a in topic.content_assets if a.status == "published"]
    return topic


@router.post("/topics", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
def create_topic(body: TopicCreate, _admin: AdminDep, db: DbDep):
    import re as _re

    existing = db.scalar(select(Topic).where(Topic.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail="Topic with this name already exists")
    data = body.model_dump()
    # Auto-generate slug if not provided
    if not data.get("slug"):
        data["slug"] = _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
    # Check slug uniqueness
    if db.scalar(select(Topic).where(Topic.slug == data["slug"])):
        raise HTTPException(status_code=409, detail="Topic with this slug already exists")
    obj = Topic(**data)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    obj = db.query(Topic).options(selectinload(Topic.children)).filter(Topic.id == obj.id).one()
    return obj


@router.patch("/topics/{topic_id}", response_model=TopicOut)
def update_topic(topic_id: uuid.UUID, body: TopicUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    obj = db.query(Topic).options(selectinload(Topic.children)).filter(Topic.id == obj.id).one()
    return obj


@router.delete("/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_topic(topic_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    obj = db.get(Topic, topic_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Topic not found")
    db.delete(obj)
    db.commit()


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
            selectinload(ContentAsset.topics),
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
    """Return hand-picked resources or auto-fallback by shared objectives/topics."""
    if asset.resources:
        return list(asset.resources)

    obj_ids = [o.id for o in asset.objectives]
    topic_ids = [t.id for t in asset.topics]
    if not obj_ids and not topic_ids:
        return []

    from src.content.models import ContentAssetObjective as CAO, ContentAssetTopic as CAT

    subq_obj = select(CAO.content_asset_id).where(CAO.objective_id.in_(obj_ids))
    subq_top = select(CAT.content_asset_id).where(CAT.topic_id.in_(topic_ids))
    stmt = (
        select(ContentAsset)
        .options(selectinload(ContentAsset.asset_type))
        .where(
            ContentAsset.status == "published",
            ContentAsset.id != asset.id,
            or_(
                ContentAsset.id.in_(subq_obj),
                ContentAsset.id.in_(subq_top),
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
    if topic_id:
        stmt = stmt.join(ContentAssetTopic).where(ContentAssetTopic.topic_id == topic_id)
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
    topic_id: Annotated[uuid.UUID | None, Query()] = None,
    topic_ids: Annotated[str | None, Query()] = None,
    cohort_id: Annotated[uuid.UUID | None, Query()] = None,
    is_featured: Annotated[bool | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "created_at",
    sort_dir: Annotated[str, Query()] = "desc",
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Public endpoint — only returns published assets.

    Supports multi-value filtering via comma-separated ID params
    (e.g. ``objective_ids=id1,id2``).  Single-value params are kept
    for backward compatibility.
    """
    # Fuzzy search uses pg_trgm word_similarity() on name and description.
    # Name matches are weighted 2x higher than description matches so that
    # assets *about* the search term rank above those that merely mention it.
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
        # Filter: must have a meaningful match in name OR description
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

    # Topic filtering (single or multi)
    t_ids = _parse_csv_uuids(topic_ids)
    if t_ids:
        stmt = stmt.join(ContentAssetTopic).where(ContentAssetTopic.topic_id.in_(t_ids))
    elif topic_id:
        stmt = stmt.join(ContentAssetTopic).where(ContentAssetTopic.topic_id == topic_id)

    if cohort_id:
        stmt = stmt.join(ContentAssetCohort).where(ContentAssetCohort.cohort_id == cohort_id)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.scalar(count_stmt)

    # When searching, order by relevance — name matches weighted 2x.
    # Otherwise honour the caller's sort preference.
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


@router.put("/assets/{asset_id}/topics", response_model=ContentAssetDetail)
def update_asset_topics(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetTopic).filter_by(content_asset_id=asset_id).delete()
    for tid in body.ids:
        db.add(ContentAssetTopic(content_asset_id=asset_id, topic_id=tid))
    db.commit()
    return _load_asset_detail(db, asset_id)


@router.put("/assets/{asset_id}/workshops", response_model=ContentAssetDetail)
def update_asset_workshops(asset_id: uuid.UUID, body: RelationshipsUpdate, _admin: AdminDep, db: DbDep):
    obj = db.get(ContentAsset, asset_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Content asset not found")
    db.query(ContentAssetWorkshop).filter_by(content_asset_id=asset_id).delete()
    for wid in body.ids:
        db.add(ContentAssetWorkshop(content_asset_id=asset_id, workshop_id=wid))
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


# ── Grade Configs ────────────────────────────────────────────────────────────


def _load_grade_config(db, gc: GradeConfig) -> GradeConfigOut:
    """Build a GradeConfigOut with topics and their published assets."""
    topics_with_assets = []
    for topic in gc.topics:
        published = [
            a for a in topic.content_assets if a.status == "published"
        ]
        topics_with_assets.append(
            TopicWithAssets(
                id=topic.id,
                name=topic.name,
                description=topic.description,
                icon_url=topic.icon_url,
                slug=topic.slug,
                suggested_grades=topic.suggested_grades,
                sort_order=topic.sort_order,
                content_assets=[ContentAssetSummary.model_validate(a) for a in published],
            )
        )
    return GradeConfigOut(
        id=gc.id,
        grade=gc.grade,
        label=gc.label,
        description=gc.description,
        video_overview_url=gc.video_overview_url,
        icon=gc.icon,
        bg_color=gc.bg_color,
        sort_order=gc.sort_order,
        topics=topics_with_assets,
        created_at=gc.created_at,
    )


@router.get("/grade-configs/public", response_model=list[GradeConfigOut])
def list_grade_configs_public(db: DbDep):
    """Public: list all grade configs with their topics and published assets."""
    configs = (
        db.query(GradeConfig)
        .options(
            selectinload(GradeConfig.topics).selectinload(Topic.content_assets).selectinload(ContentAsset.asset_type),
        )
        .order_by(GradeConfig.grade)
        .all()
    )
    return [_load_grade_config(db, gc) for gc in configs]


@router.get("/grade-configs", response_model=list[GradeConfigSummary])
def list_grade_configs(_admin: AdminDep, db: DbDep):
    """Admin: list all grade configs (lightweight, with topic IDs only)."""
    configs = (
        db.query(GradeConfig)
        .options(selectinload(GradeConfig.topics))
        .order_by(GradeConfig.grade)
        .all()
    )
    results = []
    for gc in configs:
        results.append(
            GradeConfigSummary(
                id=gc.id,
                grade=gc.grade,
                label=gc.label,
                description=gc.description,
                video_overview_url=gc.video_overview_url,
                icon=gc.icon,
                bg_color=gc.bg_color,
                sort_order=gc.sort_order,
                topic_ids=[t.id for t in gc.topics],
            )
        )
    return results


@router.post("/grade-configs", response_model=GradeConfigSummary, status_code=201)
def create_grade_config(body: GradeConfigCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a new grade config."""
    existing = db.query(GradeConfig).filter(GradeConfig.grade == body.grade).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Grade {body.grade} config already exists")
    gc = GradeConfig(**body.model_dump())
    db.add(gc)
    db.commit()
    db.refresh(gc)
    return GradeConfigSummary(
        id=gc.id, grade=gc.grade, label=gc.label,
        description=gc.description, video_overview_url=gc.video_overview_url,
        icon=gc.icon, bg_color=gc.bg_color, sort_order=gc.sort_order,
        topic_ids=[],
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
    gc = db.query(GradeConfig).options(selectinload(GradeConfig.topics)).filter(GradeConfig.id == gc.id).one()
    return GradeConfigSummary(
        id=gc.id, grade=gc.grade, label=gc.label,
        description=gc.description, video_overview_url=gc.video_overview_url,
        icon=gc.icon, bg_color=gc.bg_color, sort_order=gc.sort_order,
        topic_ids=[t.id for t in gc.topics],
    )


@router.delete("/grade-configs/{grade_config_id}", status_code=204)
def delete_grade_config(grade_config_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: delete a grade config."""
    gc = db.get(GradeConfig, grade_config_id)
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")
    db.delete(gc)
    db.commit()


@router.put("/grade-configs/{grade_config_id}/topics", response_model=GradeConfigSummary)
def update_grade_config_topics(
    grade_config_id: uuid.UUID, body: GradeConfigTopicsUpdate, _admin: AdminDep, db: DbDep
):
    """Admin: replace the topics assigned to a grade config. Only top-level topics allowed."""
    gc = db.get(GradeConfig, grade_config_id)
    if not gc:
        raise HTTPException(status_code=404, detail="Grade config not found")

    # Validate: only top-level topics (no parent) can be assigned to grades
    if body.topic_ids:
        sub_topics = (
            db.query(Topic)
            .filter(Topic.id.in_(body.topic_ids), Topic.parent_id.isnot(None))
            .all()
        )
        if sub_topics:
            names = ", ".join(t.name for t in sub_topics)
            raise HTTPException(status_code=400, detail=f"Only top-level topics can be assigned to grades. Sub-topics found: {names}")

    # Clear existing
    db.query(GradeConfigTopic).filter(GradeConfigTopic.grade_config_id == grade_config_id).delete()

    # Insert new with sort order
    for i, topic_id in enumerate(body.topic_ids):
        db.add(GradeConfigTopic(grade_config_id=grade_config_id, topic_id=topic_id, sort_order=i))
    db.commit()

    gc = db.query(GradeConfig).options(selectinload(GradeConfig.topics)).filter(GradeConfig.id == gc.id).one()
    return GradeConfigSummary(
        id=gc.id, grade=gc.grade, label=gc.label,
        description=gc.description, video_overview_url=gc.video_overview_url,
        icon=gc.icon, bg_color=gc.bg_color, sort_order=gc.sort_order,
        topic_ids=[t.id for t in gc.topics],
    )
