"""The "actionable" rule exists as SQL and as Python — they must agree."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.surveys.actionable import actionable_clause, is_actionable
from src.surveys.models import SurveyResponse
from tests.surveys.conftest import add_response

CASES = [
    # (fields, expected actionable)
    ({"rating_thumbs": True}, False),
    ({"rating_thumbs": False}, True),
    ({"rating_thumbs": True, "comment": "Loved the examples"}, True),
    ({"rating_thumbs": None, "question_type": "stars", "rating_stars": 2}, True),
    ({"rating_thumbs": None, "question_type": "stars", "rating_stars": 3}, False),
    ({"rating_thumbs": None, "question_type": "stars", "rating_stars": 5}, False),
    ({"rating_thumbs": None, "question_type": "text", "comment": "Where is the recording?"}, True),
    ({"rating_thumbs": None, "question_type": "text", "comment": "   "}, False),
    ({"rating_thumbs": True, "comment": ""}, False),
    # SQL trim() strips spaces only; the Python mirror must agree
    ({"rating_thumbs": True, "comment": "\n"}, True),
]


def test_whitespace_only_comment_is_dropped_on_submit():
    from src.surveys.schemas import SurveyResponseCreate

    body = SurveyResponseCreate(
        page_type="resource", page_url="/x", question_type="text", question_text="?", comment=" \n\t "
    )
    assert body.comment is None


@pytest.mark.parametrize(("fields", "expected"), CASES)
def test_python_predicate(fields, expected):
    assert (
        is_actionable(
            comment=fields.get("comment"),
            rating_thumbs=fields.get("rating_thumbs"),
            rating_stars=fields.get("rating_stars"),
        )
        is expected
    )


def test_sql_clause_matches_python_predicate(survey_sessionmaker):
    session = survey_sessionmaker()
    expected_ids = set()
    for fields, expected in CASES:
        row = add_response(session, **fields)
        if expected:
            expected_ids.add(row.id)

    matched = set(session.scalars(select(SurveyResponse.id).where(actionable_clause())))
    assert matched == expected_ids
