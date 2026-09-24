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
            return {
                "id": 81276546458,
                "registrant_id": "CUHCqHCFRC-srNag0ZwDZQ",
                "join_url": "https://us06web.zoom.us/w/81276546458?tk=personal",
            }

    monkeypatch.setattr(zoom.settings, "zoom_account_id", "acct")
    monkeypatch.setattr(zoom.settings, "zoom_client_id", "client")
    monkeypatch.setattr(zoom.settings, "zoom_client_secret", "secret")
    monkeypatch.setattr(zoom, "_get_access_token", lambda: "token")
    monkeypatch.setattr(
        zoom, "_resolve_questions", lambda *_: {"grade": None, "school": None, "questions": None}
    )
    monkeypatch.setattr(zoom.httpx, "post", lambda *a, **k: _Resp())

    assert zoom.register_webinar("81276546458", "parent@example.com", "A", "B") == (
        zoom.ZoomRegistrant(
            "CUHCqHCFRC-srNag0ZwDZQ", "https://us06web.zoom.us/w/81276546458?tk=personal"
        )
    )


class _FakeResponse:
    """Enough of ``httpx.Response`` for the registration path to act on."""

    def __init__(self, status_code: int, body: dict, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text
        self.is_error = status_code >= 400

    def raise_for_status(self) -> None:
        if self.is_error:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body


def _stub_credentials(monkeypatch) -> None:
    from src.integrations import zoom

    monkeypatch.setattr(zoom.settings, "zoom_account_id", "acct")
    monkeypatch.setattr(zoom.settings, "zoom_client_id", "client")
    monkeypatch.setattr(zoom.settings, "zoom_client_secret", "secret")
    monkeypatch.setattr(zoom, "_get_access_token", lambda: "token")
    monkeypatch.setattr(
        zoom, "_resolve_questions", lambda *_: {"grade": None, "school": None, "questions": None}
    )


_REFUSAL = "The parameter is required in custom_questions: Which school does your student attend?."


def test_a_custom_questions_refusal_relaxes_the_form_and_retries(monkeypatch):
    """The registration is sound; only the form in front of it is wrong."""
    from src.integrations import zoom

    _stub_credentials(monkeypatch)
    attempts: list[int] = []
    relaxed: list[str] = []

    def fake_post(*_a, **_k):
        attempts.append(1)
        if len(attempts) == 1:
            return _FakeResponse(400, {}, _REFUSAL)
        return _FakeResponse(201, {"id": 81276546458, "registrant_id": "real-id"})

    monkeypatch.setattr(zoom.httpx, "post", fake_post)
    monkeypatch.setattr(
        zoom, "_relax_custom_questions", lambda wid, _t: bool(relaxed.append(wid)) or True
    )

    registrant = zoom.register_webinar("81276546458", "parent@example.com", "A", "B")
    assert registrant is not None and registrant.registrant_id == "real-id"
    assert len(attempts) == 2
    assert relaxed == ["81276546458"]


def test_a_refusal_about_anything_else_is_not_retried(monkeypatch):
    """Only a questions refusal is worth rewriting the webinar's form for."""
    from src.integrations import zoom

    _stub_credentials(monkeypatch)
    attempts: list[int] = []

    def fake_post(*_a, **_k):
        attempts.append(1)
        return _FakeResponse(400, {}, "Webinar host can not register for the webinar.")

    monkeypatch.setattr(zoom.httpx, "post", fake_post)
    monkeypatch.setattr(
        zoom,
        "_relax_custom_questions",
        lambda *_a: pytest.fail("must not rewrite the form for an unrelated refusal"),
    )

    assert zoom.register_webinar("81276546458", "host@example.com", "A", "B") is None
    assert len(attempts) == 1


def test_an_already_optional_form_is_not_retried(monkeypatch):
    """Nothing was relaxed, so the same payload would be refused again."""
    from src.integrations import zoom

    _stub_credentials(monkeypatch)
    attempts: list[int] = []

    def fake_post(*_a, **_k):
        attempts.append(1)
        return _FakeResponse(400, {}, _REFUSAL)

    monkeypatch.setattr(zoom.httpx, "post", fake_post)
    monkeypatch.setattr(zoom, "_relax_custom_questions", lambda *_a: False)

    assert zoom.register_webinar("81276546458", "parent@example.com", "A", "B") is None
    assert len(attempts) == 1


def test_relaxing_clears_required_and_drops_empty_answer_lists(monkeypatch):
    """Zoom returns ``answers: []`` on free text but refuses that field on write."""
    from src.integrations import zoom

    config = {
        "questions": [{"field_name": "last_name", "required": True}],
        "custom_questions": [
            {"title": "School?", "type": "single_dropdown", "required": True, "answers": ["A"]},
            {"title": "Anything to ask?", "type": "short", "required": True, "answers": []},
        ],
    }
    sent: dict = {}

    monkeypatch.setattr(zoom.httpx, "get", lambda *a, **k: _FakeResponse(200, config))
    monkeypatch.setattr(
        zoom.httpx,
        "patch",
        lambda *a, **k: sent.update(k["json"]) or _FakeResponse(204, {}),
    )

    assert zoom._relax_custom_questions("81276546458", "token") is True
    assert [q["required"] for q in sent["custom_questions"]] == [False, False]
    assert sent["custom_questions"][0]["answers"] == ["A"]
    assert "answers" not in sent["custom_questions"][1]
    # Standard fields travel back untouched — Zoom replaces the whole set.
    assert sent["questions"] == config["questions"]


def test_relaxing_a_form_with_nothing_required_changes_nothing(monkeypatch):
    from src.integrations import zoom

    config = {"questions": [], "custom_questions": [{"title": "School?", "required": False}]}
    monkeypatch.setattr(zoom.httpx, "get", lambda *a, **k: _FakeResponse(200, config))
    monkeypatch.setattr(
        zoom.httpx, "patch", lambda *a, **k: pytest.fail("must not write an unchanged form")
    )

    assert zoom._relax_custom_questions("81276546458", "token") is False
