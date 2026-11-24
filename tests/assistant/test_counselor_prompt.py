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
    assert '"our next workshop" is the one marked "(next upcoming)": treat it as named without asking' in prompt
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


def test_sources_get_absolute_links_only_with_an_origin_and_a_page():
    page = replace(make_chunk(), url_path="/school/a-high/topic/divorce")
    private = replace(make_chunk(), title="Counselor Guide", url_path=None)
    prompt = build_counselor_prompt([page, private], SCHOOL, WORKSHOPS, origin="https://cmm.example")
    assert "Link: https://cmm.example/school/a-high/topic/divorce" in prompt
    assert prompt.count("Link: ") == 1
    # A counselor-only page has no Resource Center page: background, never numbered.
    assert "[2] Counselor Guide" not in prompt and "- Counselor Guide" in prompt
    assert "Link: " not in _prompt([page])


def test_reply_rule_keeps_password_tag_and_forbids_invented_links():
    prompt = _prompt()
    assert "{{resource_center_password}} (keep that tag exactly as written)" in prompt
    assert "never invent, guess or shorten a URL" in prompt


def test_honest_workshop_figures_and_no_school_reach():
    prompt = _prompt()
    assert "even when they are low; never round up, soften or guess" in prompt
    assert "Your school's program" not in prompt and "families reached" not in prompt


def test_figures_are_workshop_totals_and_school_numbers_point_to_hub_analytics():
    prompt = build_counselor_prompt([make_chunk()], SCHOOL, WORKSHOPS, origin="https://cmm.example")
    assert '"Total registrations for this workshop: 42"' in prompt and "never call it the school's" in prompt
    assert "from the Summary line; never count or rank them yourself" in prompt
    assert "only from its own grade label" in prompt
    assert "the Assistant doesn't have school-level numbers" in prompt
    assert "[Counselor Hub Analytics page](https://cmm.example/hub/analytics)" in prompt


def test_greeting_offers_hub_help_from_the_user_guide():
    assert "how to use the Counselor Hub itself, which the Counselor Hub user guide covers" in _prompt()


def test_grade_checklist_rule_present():
    prompt = _prompt()
    assert 'grouped under month names, five to twelve "- [ ] " items' in prompt
    assert "if no grade is given, ask which grade" in prompt
    assert "If the sources say little about that grade, say so plainly" in prompt
    assert "A checklist is not an email draft: it uses no {{merge_tags}} except {{resource_center_password}}" in prompt
    assert "Leave out any item you cannot tie to a source, background note or workshop" in prompt


def test_answer_format_short_first_copy_blocks_and_link_text():
    prompt = _prompt()
    assert "a line containing only <!-- more -->" in prompt
    assert "opens with a line ```copy" in prompt and "no [n] citations" in prompt
    assert 'never "here", "this link" or a bare URL' in prompt


def test_no_answer_points_outside_and_offers_paul_by_email_to_the_counselor_only():
    prompt = _prompt()
    assert "https://studentaid.gov" in prompt and "https://cssprofile.collegeboard.org" in prompt
    assert "Common Data Set" in prompt
    assert "mailto:paul.martin@collegemoneymethod.com" in prompt
    family = build_counselor_prompt([make_chunk()], SCHOOL, WORKSHOPS, offer_paul=False)
    assert "mailto:" not in family and "email Paul" not in family
    assert "https://studentaid.gov" in family


def test_family_advising_card_turns_off_the_paul_offer(monkeypatch):
    from src.assistant import counselor_context

    seen = {}
    monkeypatch.setattr(counselor_context, "build_counselor_prompt", lambda *a, **k: seen.update(k) or "")
    monkeypatch.setattr(counselor_context, "school_workshop_section", lambda *a: "")
    monkeypatch.setattr(counselor_context, "workshop_template_section", lambda *a: "")
    monkeypatch.setattr(counselor_context, "resolve_counselor_name", lambda *a: ("Jane", "", "Jane"))
    for card, offered in (("family_advising", False), (None, True), ("grade_checklist", True)):
        counselor_context.counselor_prompt_builder(None, SCHOOL, None, guidance="", card_type=card)([])
        assert seen["offer_paul"] is offered


def test_no_financial_advice_and_student_details_not_repeated():
    prompt = _prompt()
    assert "not personal financial advice" in prompt and "a financial professional should advise" in prompt
    assert "never repeat it" in prompt and "not to enter student names or IDs" in prompt


def test_rules_cover_clarifying_and_handing_off():
    prompt = _prompt()
    assert "ask for it in one to three short questions" in prompt
    assert "say plainly who handles the rest" in prompt
    assert "Never state a price, fee, rate, payment term" in prompt


def test_card_line_follows_who_is_asking_only_when_given():
    from src.assistant.counselor_card_context import CARD_CONTEXT, card_prompt

    line = card_prompt("family_advising")
    prompt = build_counselor_prompt([make_chunk()], SCHOOL, WORKSHOPS, card=line)
    assert f"a high school counselor at A High (TX).\n{line}\n\nRules:" in prompt
    assert "started from" not in _prompt()
    assert card_prompt(None) == card_prompt("unknown") == ""
    assert all(c.prompt for c in CARD_CONTEXT.values())


def test_answers_keep_first_person_out():
    prompt = _prompt()
    assert 'avoid "I", "me" and "my", and never open with them' in prompt
    assert "CMM's resources don't cover X" in prompt
    assert "still give the common-case answer before any question" in prompt


def test_gaps_lead_with_cmm_resources_and_never_suggest_one_on_one_advising():
    prompt = _prompt()
    assert "lead with what CMM does have" in prompt
    assert "Never suggest or offer a 1:1" in prompt
    assert "even where a source or the team guidance below mentions one" in prompt
