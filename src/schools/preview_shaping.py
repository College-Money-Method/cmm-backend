"""What a preview school may see of the resource center.

A preview school (self-serve prospect, not yet a customer) gets a taste: one
topic per grade, the public unrestricted resource tier, and placeholder
workshops. Shaping happens server-side; the client is never trusted to hide
anything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.content.models import (
    ContentAsset,
    ContentAssetCohort,
    ContentAssetSchool,
    ContentAssetState,
    GradeConfig,
    GradeConfigGoal,
    GradeSet,
    Topic,
)
from src.content.schemas import GradeConfigOut
from src.schools.models import School
from src.workshops.models import Workshop
from src.workshops.schemas import WorkshopPortalItem

PREVIEW_WORKSHOP_COUNT = 6
PREVIEW_ID_PREFIX = "preview-"


def preview_allowed_topic_ids(db, school: School) -> set[uuid.UUID]:
    """The first published topic of each grade config in the school's grade set.

    "First" follows what the visitor sees: goal order, then topic sort order,
    then title.
    """
    grade_set_id = school.grade_set_id
    if grade_set_id is None:
        default = db.query(GradeSet).filter(GradeSet.is_default.is_(True)).first()
        grade_set_id = default.id if default else None
    if grade_set_id is None:
        return set()

    rows = db.execute(
        select(GradeConfig.id, Topic.id)
        .join(GradeConfigGoal, GradeConfigGoal.grade_config_id == GradeConfig.id)
        .join(Topic, Topic.goal_id == GradeConfigGoal.goal_id)
        .where(GradeConfig.grade_set_id == grade_set_id, Topic.status == "published")
        .order_by(GradeConfigGoal.sort_order, Topic.sort_order, Topic.title)
    ).all()
    allowed: dict[uuid.UUID, uuid.UUID] = {}
    for config_id, topic_id in rows:
        allowed.setdefault(config_id, topic_id)
    return set(allowed.values())


def lock_grade_config(config: GradeConfigOut, allowed: set[uuid.UUID]) -> GradeConfigOut:
    """Flag every topic as locked or not and blank the locked ones."""
    for goal in config.goals:
        for topic in goal.topics:
            if topic.id in allowed:
                continue
            topic.locked = True
            topic.description = None
            topic.image_url = None
            topic.read_time_minutes = None
            topic.updated_at = None
    return config


def preview_asset_conditions():
    """SQL conditions for the preview resource tier: public and unrestricted."""
    return [
        ContentAsset.is_public.is_(True),
        ~ContentAsset.id.in_(select(ContentAssetCohort.content_asset_id)),
        ~ContentAsset.id.in_(select(ContentAssetState.content_asset_id)),
        ~ContentAsset.id.in_(select(ContentAssetSchool.content_asset_id)),
    ]


def asset_in_preview_tier(asset: ContentAsset) -> bool:
    return bool(asset.is_public) and not (asset.cohorts or asset.states or asset.schools)


def preview_workshop_id(n: int) -> str:
    return f"{PREVIEW_ID_PREFIX}{n}"


def _placeholder(school: School, workshop: Workshop) -> WorkshopPortalItem:
    n = workshop.sequence_number
    return WorkshopPortalItem(
        portal_mapping_id=uuid.uuid5(school.id, preview_workshop_id(n)),
        webinar_id=preview_workshop_id(n),
        is_preview=True,
        start_datetime=None,
        end_datetime=None,
        registration_url=None,
        zoom_link=None,
        video_embed_code=None,
        join_url=None,
        show_zoom=False,
        workshop_id=workshop.id,
        name=workshop.name,
        description=workshop.description,
        key_actions=None,
        body=None,
        suggested_grades=workshop.suggested_grades,
        workshop_art_url=workshop.workshop_art_url,
        sequence_number=n,
    )


def preview_workshops(db, school: School) -> list[WorkshopPortalItem]:
    """One placeholder per numbered workshop (1..6), ascending."""
    workshops = (
        db.query(Workshop)
        .filter(Workshop.sequence_number.between(1, PREVIEW_WORKSHOP_COUNT))
        .order_by(Workshop.sequence_number)
        .all()
    )
    return [_placeholder(school, w) for w in workshops]


def preview_workshop(db, school: School, webinar_id: str) -> WorkshopPortalItem | None:
    return next((i for i in preview_workshops(db, school) if i.webinar_id == webinar_id), None)
