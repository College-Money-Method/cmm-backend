"""Counselor prompt: rules text, school, workshop section, sources."""

from __future__ import annotations

import uuid

from src.assistant.counselor_prompt import build_counselor_prompt
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


def test_merge_tags_stay_literal_without_named_workshop():
    prompt = _prompt()
    assert "keep the {{merge_tags}} literal" in prompt
    assert "{{family_label}}" in prompt


def test_never_claims_sent_and_points_to_communications():
    prompt = _prompt()
    assert "paste it into Communications" in prompt
    assert "Never claim that anything was sent, saved" in prompt


def test_capability_question_answered_as_outcomes_not_rules():
    prompt = _prompt()
    assert "asks what you can do or how you can help" in prompt
    assert "do not describe your sources, rules or limits" in prompt
