"""Pydantic schemas for the global app config API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from src.paul_booking.hours import validate_weekly_hours
from src.schools.display_timezone import DisplayTimezoneField

# The address of the page an admin is looking at when they pick a folder. The
# API wants /users/<user>/projects/<folder>, which shares the two ids and
# nothing else, so the natural copy-paste is accepted and converted rather than
# rejected — the alternative is an audit run that queues, downloads, trims,
# samples, and only then fails at the upload.
_BROWSER_FOLDER_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?vimeo\.com/user/(\d+)/folder/(\d+)", re.IGNORECASE
)


def validate_vimeo_folder_uri(value: str | None) -> str | None:
    """Normalise a Vimeo folder reference to the API URI the pipeline uses."""
    if value is None:
        return None
    cleaned = value.strip().rstrip("/")
    if not cleaned:
        return None
    browser = _BROWSER_FOLDER_URL.match(cleaned)
    if browser:
        return f"/users/{browser.group(1)}/projects/{browser.group(2)}"
    if not cleaned.startswith("/"):
        raise ValueError(
            "Paste the folder's Vimeo URL, or its API URI "
            "(/users/<user>/projects/<folder>)."
        )
    return cleaned


# Cleaned and shape-checked on the way in, so the pipeline never has to cope
# with a folder reference Vimeo cannot resolve.
VimeoFolderUriField = Annotated[str | None, AfterValidator(validate_vimeo_folder_uri)]

_EMAIL = TypeAdapter(EmailStr)


def validate_optional_email(value: str | None) -> str | None:
    """Blank clears the value; anything else must be one valid address."""
    cleaned = (value or "").strip()
    return str(_EMAIL.validate_python(cleaned)) if cleaned else None


OptionalEmailField = Annotated[str | None, AfterValidator(validate_optional_email)]


def validate_optional_web_url(value: str | None) -> str | None:
    """Blank clears the value; anything else must be an absolute http(s) URL."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    parsed = urlsplit(cleaned)
    bad = (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None  # no embedded credentials
        or any(ch.isspace() for ch in cleaned)
        or len(cleaned) > 2000
    )
    if bad:
        raise ValueError("Enter a full web address starting with https://")
    return cleaned


# Rendered as a link on public pages, so only web schemes (never javascript:).
OptionalWebUrlField = Annotated[str | None, AfterValidator(validate_optional_web_url)]


class AppConfigUpdate(BaseModel):
    """PATCH payload — all fields optional. Pass null to clear a value."""

    welcome_video_embed_code: str | None = None
    welcome_video_title: str | None = None
    welcome_video_caption: str | None = None
    topic_overview_video_url: str | None = None
    # Blank clears the app-wide default, falling back to the env seed.
    workshop_display_timezone: DisplayTimezoneField = None
    # Blank clears the override, falling back to the env seed.
    vimeo_audit_folder_uri: VimeoFolderUriField = None
    vimeo_replay_folder_uri: VimeoFolderUriField = None
    vimeo_reel_folder_uri: VimeoFolderUriField = None
    survey_enabled: bool | None = None
    email_sandbox_mode: bool | None = None
    # "Request a call with Paul". The zone is validated like the workshop one;
    # blank clears it (Pacific is then used). Hours: weekday keys mon..sun, each
    # null or {start, end} as HH:MM on the half hour with start before end.
    paul_booking_enabled: bool | None = None
    paul_booking_timezone: DisplayTimezoneField = None
    paul_booking_weekly_hours: Annotated[dict | None, AfterValidator(validate_weekly_hours)] = None
    # Who gets the review and confirmation emails; blank = Paul's own address.
    paul_booking_notify_email: OptionalEmailField = None
    # College Aid Explorer "Find your SAI" link; blank hides the link.
    college_aid_sai_calculator_url: OptionalWebUrlField = None

    @field_validator("paul_booking_enabled")
    @classmethod
    def _enabled_not_null(cls, value: bool | None) -> bool:
        # The column is NOT NULL: an explicit null would fail at commit as a 500.
        if value is None:
            raise ValueError("paul_booking_enabled must be true or false")
        return value


class CollegeAidPasswordIn(BaseModel):
    """PUT body for the College Aid Explorer shared password (never echoed back)."""

    password: str = Field(min_length=8, max_length=200, repr=False)


class AssistantPasswordIn(CollegeAidPasswordIn):
    """PUT body for the AI assistant shared password (same rules, never echoed back)."""


# Password flags derived from hash columns; the hashes are never schema fields.
_DERIVED_FLAGS = {"college_aid_password_set", "assistant_password_set"}


class AppConfigOut(BaseModel):
    # This API is public: the password hash is deliberately not a field here,
    # only whether one is set. Keep it that way.
    id: uuid.UUID
    welcome_video_embed_code: str | None
    welcome_video_title: str | None
    welcome_video_caption: str | None
    topic_overview_video_url: str | None
    workshop_display_timezone: str | None
    vimeo_audit_folder_uri: str | None
    vimeo_replay_folder_uri: str | None
    vimeo_reel_folder_uri: str | None
    survey_enabled: bool
    email_sandbox_mode: bool
    paul_booking_enabled: bool
    paul_booking_timezone: str | None
    paul_booking_weekly_hours: dict | None
    paul_booking_notify_email: str | None
    college_aid_password_updated_at: datetime | None = None
    college_aid_sai_calculator_url: str | None = None
    assistant_password_updated_at: datetime | None = None
    updated_at: datetime | None

    # Derived from the ORM hash column in the before-validator below; the hash
    # itself is never a field of this schema.
    college_aid_password_set: bool = False
    assistant_password_set: bool = False

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def _derive_password_flag(cls, data):
        if isinstance(data, dict):
            return data
        return {
            **{name: getattr(data, name, None) for name in cls.model_fields if name not in _DERIVED_FLAGS},
            "college_aid_password_set": bool(getattr(data, "college_aid_password_hash", None)),
            "assistant_password_set": bool(getattr(data, "assistant_password_hash", None)),
        }
