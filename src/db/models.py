"""Barrel re-export of all SQLAlchemy ORM models and enums.

Import all feature models here so that:
  - Alembic discovers every table via ``Base.metadata``
  - Scripts and services can do: ``from src.db.models import School, Cycle, ...``
"""

from src.db.enums import CycleStatus, ProposalType, RegistrationStatus, SalesStatus

from src.app_config.models import AppConfig
from src.auth.models import Profile, UserRole
from src.assets.models import Asset
from src.guest_contacts.models import GuestContact
from src.calculators.models import Calculator
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
from src.content.video_caption_models import VideoCaptionRecord
from src.cycles.models import Cohort, Cycle
from src.meetings.models import OneOnOneMeeting
from src.sales.models import Invoice, Sale
from src.schools.models import Contact, School, SchoolDateSelector
from src.settings.models import Setting
from src.storage.models import StorageFile
from src.video_pipeline.models import WebinarVideoJob
from src.workshops.models import PortalMapping, Webinar, Workshop, WorkshopAsset, WorkshopRegistration
from src.workshops.qa_models import (
    WebinarQaAnswerExtraction,
    WebinarQaQuestion,
    WebinarQaSync,
)

__all__ = [
    # Enums
    "CycleStatus",
    "ProposalType",
    "RegistrationStatus",
    "SalesStatus",
    # Models
    "AppConfig",
    "Profile",
    "UserRole",
    "Asset",
    "Calculator",
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
    "VideoCaptionRecord",
    "Webinar",
    "WebinarQaAnswerExtraction",
    "WebinarQaQuestion",
    "WebinarQaSync",
    "WebinarVideoJob",
    "Workshop",
    "WorkshopAsset",
    "WorkshopRegistration",
]
