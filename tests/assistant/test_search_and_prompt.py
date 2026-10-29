"""Pure retrieval and prompt helpers (no database, no Bedrock)."""

from __future__ import annotations

import uuid

from src.assistant.answer import build_system_prompt, source_list, sse
from src.assistant.search import RRF_K, keyword_query, reciprocal_rank_fusion, retrieval_query
from src.assistant.sync import embedding_input
from tests.assistant.conftest import make_chunk


def test_rrf_rewards_items_on_both_lists():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scores = reciprocal_rank_fusion([a, b], [b, c])
    assert max(scores, key=scores.get) == b
    assert scores[a] == 1 / (RRF_K + 1)
    assert scores[b] == 1 / (RRF_K + 2) + 1 / (RRF_K + 1)


def test_rrf_empty():
    assert reciprocal_rank_fusion([], []) == {}


def test_keyword_query_ors_words_and_drops_punctuation():
    assert keyword_query("We're divorced — who files the FAFSA?") == "divorced | fafsa | files | re | the | we | who"
    assert keyword_query("?!") == ""


def test_retrieval_query_keeps_subject_through_a_clarifying_round():
    assert retrieval_query([], "Who files the FAFSA?") == "Who files the FAFSA?"
    history = [
        {"role": "user", "content": "Who files the FAFSA?"},
        {"role": "assistant", "content": "It depends. Are your parents divorced?"},
        {"role": "user", "content": "yes"},
        {"role": "assistant", "content": "Who did you live with more last year?"},
    ]
    assert retrieval_query(history, "my mom") == "Who files the FAFSA?\nyes\nmy mom"
    assert retrieval_query(history[2:], "my mom") == "yes\nmy mom"


def test_prompt_asks_one_follow_up_without_personal_data():
    prompt = build_system_prompt([], {"role": "family", "grade": None, "state": None})
    assert "end with one brief follow-up question" in prompt
    assert "Never ask for names, contact details, income or asset figures" in prompt


def test_prompt_carries_profile_and_numbered_sources():
    chunks = [make_chunk(), make_chunk(title="CSS Profile", heading=None, url=None)]
    prompt = build_system_prompt(chunks, {"role": "family", "grade": 11, "state": "TX"})
    assert "a parent or student of a junior (11th grade), in TX." in prompt
    assert "[1] Divorced and Separated Parents — Who files" in prompt
    assert "[2] CSS Profile\n" in prompt


def test_prompt_for_counselor_and_no_sources():
    prompt = build_system_prompt([], {"role": "counselor", "grade": None, "state": None})
    assert "high school counselors" in prompt and "a high school counselor." in prompt
    assert "(no sources matched this question)" in prompt


def test_source_list_numbers_match_prompt():
    sources = source_list([make_chunk(), make_chunk(title="Loans", url=None)])
    assert [s["n"] for s in sources] == [1, 2]
    assert sources[1] == {"n": 2, "title": "Loans", "url": None, "source_type": "topic", "heading": "Who files"}


def test_sse_frame():
    assert sse("delta", {"text": "hé"}) == 'event: delta\ndata: {"text": "hé"}\n\n'


def test_embedding_input_includes_questions():
    text = embedding_input("Title", None, "Body", "Q1?\nQ2?")
    assert text == "Title\n\nBody\n\nQuestions this answers:\nQ1?\nQ2?"
