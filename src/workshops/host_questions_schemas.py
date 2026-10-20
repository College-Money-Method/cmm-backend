"""Response shapes for the public co-host questions page and the admin host-link.

This payload is readable by anyone holding the link, and hosts span every school
in a cohort session, so it carries no registrant identity at all: no names,
emails, schools, ids or Zoom fields — only each question, its grade and when it
was submitted. Only a registrant's own school may know who registered.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class HostWebinarOut(BaseModel):
    # webinar_name is deliberately absent: cohort session names list the schools.
    workshop_title: str | None
    start_datetime: datetime | None
    end_datetime: datetime | None
    display_timezone: str


class HostQuestionOut(BaseModel):
    question: str
    grade: str | None
    submitted_at: datetime


class HostQuestionsOut(BaseModel):
    webinar: HostWebinarOut
    ended: bool
    # Newest first, so questions from late registrations surface at the top.
    questions: list[HostQuestionOut]


class HostLinkOut(BaseModel):
    url: str
