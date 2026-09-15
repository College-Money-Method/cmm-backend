"""Zoom caps a custom-question answer at 128 characters.

The bug this pins down: a parent who wrote more than that in "Are there any
questions you would like to submit on this workshop topic?" had their whole
Zoom registration rejected with ``Invalid parameter: custom_questions``. The
failure was non-fatal, so their registration row committed as approved and they
simply never received a join link. 52 production registrations were stranded
this way — the parents who wrote the most were the ones silently dropped.
"""

from __future__ import annotations

from src.integrations.zoom import _ZOOM_ANSWER_MAX_CHARS, _match_answer

GRADES = ["9th", "10th", "11th", "12th"]


def test_free_text_answer_is_capped():
    answer = _match_answer("q" * 400, [])
    assert answer is not None
    assert len(answer) == _ZOOM_ANSWER_MAX_CHARS


def test_free_text_answer_within_the_limit_is_untouched():
    """The 98% of parents who write a normal-length question must be unaffected."""
    value = "How do we report a 529 plan on the FAFSA?"
    assert _match_answer(value, []) == value


def test_answer_at_the_limit_is_untouched():
    value = "x" * _ZOOM_ANSWER_MAX_CHARS
    assert _match_answer(value, []) == value


def test_dropdown_answers_are_returned_verbatim():
    """Capping must not corrupt a value Zoom matches against a fixed list."""
    assert _match_answer("12th", GRADES) == "12th"


def test_unmatched_dropdown_answer_is_still_none():
    assert _match_answer("Kindergarten", GRADES) is None


def test_registrant_id_is_read_from_registrant_id_not_id(monkeypatch):
    """Zoom returns the webinar id under "id" and the person's under "registrant_id".

    Reading "id" stamped the webinar id onto every registration, which made
    attendance matching by registrant id miss every time and quietly fall back
    to email.
    """
    from src.integrations import zoom

    class _Resp:
        status_code = 201

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"id": 81276546458, "registrant_id": "CUHCqHCFRC-srNag0ZwDZQ"}

    monkeypatch.setattr(zoom.settings, "zoom_account_id", "acct")
    monkeypatch.setattr(zoom.settings, "zoom_client_id", "client")
    monkeypatch.setattr(zoom.settings, "zoom_client_secret", "secret")
    monkeypatch.setattr(zoom, "_get_access_token", lambda: "token")
    monkeypatch.setattr(
        zoom, "_resolve_questions", lambda *_: {"grade": None, "school": None, "questions": None}
    )
    monkeypatch.setattr(zoom.httpx, "post", lambda *a, **k: _Resp())

    assert zoom.register_webinar("81276546458", "parent@example.com", "A", "B") == (
        "CUHCqHCFRC-srNag0ZwDZQ"
    )
