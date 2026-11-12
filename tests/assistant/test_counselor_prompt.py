"""Counselor prompt: rules text, school, workshop section, sources."""

from __future__ import annotations

import uuid
from dataclasses import replace

from src.assistant.counselor_prompt import build_counselor_prompt
from src.assistant.counselor_template_values import (
    fill_template_chunks,
    template_values,
    template_values_section,
)
from src.assistant.counselor_workshop_section import SECTION_HEADER
from src.assistant.prompt_tool_sections import CALCULATOR_MARKER, COLLEGE_AID_MARKER
from src.schools.models import School
from tests.assistant.conftest import make_chunk

SCHOOL = School(id=uuid.uuid4(), name="A High", state="TX")
WORKSHOPS = f"{SECTION_HEADER}\n- FAFSA Night: Nov 12 at 6:00 PM (12th grade); register: https://x.example/w"


def _prompt(chunks=None):
    return build_counselor_prompt(chunks if chunks is not None else [make_chunk()], SCHOOL, WORKSHOPS)


def test_names_school_and_includes_workshops_and_sources():
    prompt = _prompt()
    assert "a high school counselor at A High (TX)" in prompt
    assert WORKSHOPS in prompt
    assert "[1] Divorced and Separated Parents" in prompt


def test_empty_sources_message():
    assert "(no sources matched this question)" in _prompt([])


def test_no_calculator_or_college_aid_markers():
    prompt = _prompt()
    assert CALCULATOR_MARKER not in prompt and COLLEGE_AID_MARKER not in prompt
    assert "Calculator" not in prompt and "College Aid Explorer" not in prompt


def test_ambiguous_workshop_rule_present():
    prompt = _prompt()
    assert "do not guess: ask which one, listing the candidates by name and date" in prompt
    assert "the FAFSA workshop" in prompt


def test_workshop_tags_stay_literal_without_named_workshop():
    prompt = _prompt()
    assert "keep the workshop {{merge_tags}} literal" in prompt
    assert "{{recipient_first_names}}" in prompt


def test_template_values_and_asker_name_fill_drafts():
    values = template_values_section({"school_name": "A High", "counselor_name": "Jane Doe"})
    prompt = build_counselor_prompt([make_chunk()], SCHOOL, WORKSHOPS, "", values, "Jane Doe")
    assert "Jane Doe, a high school counselor at A High (TX)." in prompt
    assert f"{values}\n\n{WORKSHOPS}" in prompt
    assert "- {{counselor_name}}: Jane Doe" in prompt
    assert "instead of placeholders like [Your Name]" in prompt


def test_without_template_values_the_section_is_absent():
    assert "Template values (" not in _prompt()


def test_template_values_skip_secrets_recipients_and_blanks():
    school = School(id=uuid.uuid4(), name="A High", slug="a-high", cmm_website_password="pw")
    values = template_values(school, ("Jane", "", "Jane"))
    assert values["school_name"] == "A High" and values["counselor_name"] == "Jane"
    assert values["family_label"] == "A High families"
    assert "resource_center_password" not in values
    assert "recipient_first_names" not in values
    assert "counselor_last_name" not in values


def test_never_claims_sent_and_points_to_communications():
    prompt = _prompt()
    assert "paste it into Communications" in prompt
    assert "Never claim that anything was sent, saved" in prompt


def test_capability_question_answered_as_outcomes_not_rules():
    prompt = _prompt()
    assert "asks what you can do or how you can help" in prompt
    assert "do not describe your sources, rules or limits" in prompt


def test_known_tags_are_filled_into_retrieved_templates():
    template = replace(make_chunk(), source_type="template", text="Hi {{recipient_first_names}}, {{school_name}} - {{counselor_name}}")
    topic = replace(make_chunk(), text="About {{school_name}}")
    filled = fill_template_chunks([template, topic], {"school_name": "A High", "counselor_name": "Jane Doe"})
    assert filled[0].text == "Hi {{recipient_first_names}}, A High - Jane Doe"
    assert filled[1] is topic
