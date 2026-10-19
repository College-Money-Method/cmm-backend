"""Pydantic schemas for the global app config API."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, TypeAdapter, field_validator

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

    @field_validator("paul_booking_enabled")
    @classmethod
    def _enabled_not_null(cls, value: bool | None) -> bool:
        # The column is NOT NULL: an explicit null would fail at commit as a 500.
        if value is None:
            raise ValueError("paul_booking_enabled must be true or false")
        return value


class AppConfigOut(BaseModel):
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
    updated_at: datetime | None

    model_config = {"from_attributes": True}
