"""Pydantic schemas for guest contact submissions."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class GuestContactCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, max_length=120)
    school_name: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=5000)

    # Honeypot. The form renders this hidden and empty; a human never sees it,
    # so anything here means a script filled the page in. Named to look like an
    # ordinary field so a bot is tempted to complete it.
    website: str | None = Field(default=None, max_length=200, exclude=True)


class GuestContactReceipt(BaseModel):
    """What the public endpoint hands back — an acknowledgement, nothing more.

    Deliberately omits the spam verdict. Echoing ``is_spam`` would tell a bot
    the moment it was caught and which rule to work around; a quarantined
    submission has to look exactly like an accepted one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime | None = None


class GuestContactDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str
    last_name: str | None = None
    email: str
    phone: str | None = None
    role: str | None = None
    school_name: str | None = None
    message: str
    created_at: datetime | None = None
    is_spam: bool = False
    spam_reason: str | None = None
    # Null until an admin marks the enquiry answered.
    resolved_at: datetime | None = None
