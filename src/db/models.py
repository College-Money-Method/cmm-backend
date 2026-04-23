"""Barrel re-export of all SQLAlchemy ORM models and enums.

Import all feature models here so that:
  - Alembic discovers every table via ``Base.metadata``
  - Scripts and services can do: ``from src.db.models import School, Cycle, ...``
"""

from src.db.enums import CycleStatus, ProposalType, RegistrationStatus, SalesStatus

from src.assets.models import Asset
from src.guest_contacts.models import GuestContact
from src.calendar.models import PaulMartinCalendar
from src.content.models import (
    AssetType,
    ContentAsset,
    ContentAssetCohort,
    ContentAssetObjective,
    WorkshopResource,
    GradeConfig,
    GradeConfigGoal,
    GradeSet,
    Objective,
    ObjectiveWorkshop,
    Topic,
)
from src.cycles.models import Cohort, Cycle
from src.meetings.models import OneOnOneMeeting
from src.sales.models import Invoice, Sale
from src.schools.models import Contact, School, SchoolDateSelector
from src.settings.models import Setting
from src.storage.models import StorageFile
from src.workshops.models import PortalMapping, Webinar, Workshop, WorkshopAsset, WorkshopRegistration

__all__ = [
    # Enums
    "CycleStatus",
    "ProposalType",
    "RegistrationStatus",
    "SalesStatus",
    # Models
    "Asset",
    "AssetType",
    "Cohort",
    "Contact",
    "GuestContact",
    "ContentAsset",
    "ContentAssetCohort",
    "ContentAssetObjective",
    "WorkshopResource",
    "Cycle",
    "GradeConfig",
    "GradeConfigGoal",
    "GradeSet",
    "Invoice",
    "Objective",
    "ObjectiveWorkshop",
    "OneOnOneMeeting",
    "PaulMartinCalendar",
    "PortalMapping",
    "Sale",
    "School",
    "SchoolDateSelector",
    "Setting",
    "StorageFile",
    "Topic",
    "Webinar",
    "Workshop",
    "WorkshopAsset",
    "WorkshopRegistration",
]
