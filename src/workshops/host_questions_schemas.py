"""Response shapes for the public co-host questions page and the admin host-link.

``HostRegistrationOut`` is deliberately a separate, narrow model rather than a
trimmed ``RegistrationOut``: this payload is readable by anyone holding the
link, so ids, status, school ids and Zoom fields must not be reachable even by
mistake.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HostWebinarOut(BaseModel):
    webinar_name: str | None
    workshop_title: str | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    display_timezone: str


class HostRegistrationOut(BaseModel):
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str
    grade: str | None
    school_name: str | None
    questions: str | None
    registration_time: datetime | None
    created_at: datetime
    attended: bool


class HostQuestionsOut(BaseModel):
    webinar: HostWebinarOut
    ended: bool
    expires_at: datetime
    registrations: list[HostRegistrationOut]


class HostLinkOut(BaseModel):
    url: str
    expires_at: datetime | None
