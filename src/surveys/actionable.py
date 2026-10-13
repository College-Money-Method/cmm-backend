"""What makes a survey response worth an admin's attention.

A response is *actionable* when a person wrote something, or when the rating is
negative: a non-empty comment, a thumbs-down, or 2 stars or fewer. Plain
positive ratings (thumbs-up, 3–5 stars, no comment) are only counted — they
never raise the admin badge or appear as items in the digest email.

The rule exists twice — as a SQL clause for queries and as a Python predicate
for serialising rows — and both live here so they cannot drift apart. The tests
check that they agree on the same fixtures.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, func, or_

from src.surveys.models import SurveyResponse

NEGATIVE_STARS_MAX = 2


def actionable_clause() -> ColumnElement[bool]:
    """SQL condition matching actionable responses."""
    return or_(
        and_(SurveyResponse.comment.is_not(None), func.length(func.trim(SurveyResponse.comment)) > 0),
        SurveyResponse.rating_thumbs.is_(False),
        SurveyResponse.rating_stars <= NEGATIVE_STARS_MAX,
    )


def needs_review_clause() -> ColumnElement[bool]:
    """Actionable and not yet marked reviewed by any admin."""
    return and_(actionable_clause(), SurveyResponse.reviewed_at.is_(None))


def is_actionable(
    *, comment: str | None, rating_thumbs: bool | None, rating_stars: int | None
) -> bool:
    """Python mirror of :func:`actionable_clause`.

    ``strip(" ")``, not ``strip()``: SQL ``trim()`` removes only spaces, and the
    two must agree or a row could count toward the badge while the UI offers no
    way to clear it. New comments are whitespace-normalised on write anyway.
    """
    return bool(comment and comment.strip(" ")) or rating_thumbs is False or (
        rating_stars is not None and rating_stars <= NEGATIVE_STARS_MAX
    )
