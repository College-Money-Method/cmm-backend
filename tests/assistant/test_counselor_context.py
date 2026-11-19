"""The shared counselor prompt builder: admin guidance by default, an explicit override for the eval."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.assistant import counselor_context
from src.assistant.counselor_context import counselor_prompt_builder
from src.assistant.models import AssistantPromptSection
from src.assistant.prompt_sections import format_guidance
from src.schools.models import School
from src.workshops.models import WorkshopEmailTemplate
from tests.assistant.conftest import make_chunk
from tests.assistant.counselor_test_support import add_webinar, make_counselor_factory


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(counselor_context, "email_origin", lambda: "https://cmm.example")
    with make_counselor_factory()() as session:
        yield session


@pytest.fixture
def school(db):
    row = School(id=uuid.uuid4(), name="A High", slug="a-high", state="TX")
    db.add(row)
    db.add(AssistantPromptSection(title="Saved rule", body="Saved guidance body.", audience="counselor"))
    db.commit()
    return row


def test_uses_saved_guidance_and_absolute_links(db, school):
    prompt = counselor_prompt_builder(db, school, None)([make_chunk()])
    assert "### Saved rule\nSaved guidance body." in prompt
    assert "a high school counselor at A High (TX)" in prompt and "Link: https://cmm.example/" in prompt


def test_guidance_override_replaces_saved_guidance(db, school):
    override = format_guidance([("Draft rule", "Draft guidance body.")])
    prompt = counselor_prompt_builder(db, school, None, guidance=override)([make_chunk()])
    assert "### Draft rule\nDraft guidance body." in prompt and "Saved guidance body." not in prompt


def test_workshop_email_message_brings_that_workshops_template(db, school):
    webinar = add_webinar(db, school.id, datetime.now(UTC) + timedelta(days=5), name="FAFSA Live")
    db.add(WorkshopEmailTemplate(workshop_id=webinar.workshop_id, type="announcement", name="Invite",
                                 subject="Join {{workshop_name}}", body="Own invite body"))
    db.commit()
    build = counselor_prompt_builder(db, school, None, card_type="workshop_promotion", message="FAFSA Live")
    assert "Subject: Join FAFSA Live\nOwn invite body" in build([make_chunk()])
    assert "Own invite body" not in counselor_prompt_builder(db, school, None, message="FAFSA Live")([make_chunk()])


def test_blank_sections_format_to_nothing():
    assert format_guidance([("Empty", "  ")]) == ""
