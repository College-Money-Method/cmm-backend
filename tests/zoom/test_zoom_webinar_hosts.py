"""Co-host lookup: parsing, pagination, and the None-vs-[] failure contract."""

from __future__ import annotations

import logging

import httpx
import pytest

from src.integrations import zoom_webinar_hosts as hosts


def _resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("GET", "https://api.zoom.us"))


@pytest.fixture(autouse=True)
def zoom_ready(monkeypatch):
    monkeypatch.setattr(hosts, "is_configured", lambda: True)
    monkeypatch.setattr(hosts, "_get_access_token", lambda: "tok")


def _route(monkeypatch, webinar: httpx.Response, *panelist_pages: httpx.Response):
    pages = iter(panelist_pages)
    calls: list[dict] = []

    def fake_get(url, **kw):
        calls.append({"url": url, **kw})
        return webinar if url.endswith("/123") else next(pages)

    monkeypatch.setattr(hosts.httpx, "get", fake_get)
    return calls


def test_splits_dedupes_and_lowercases(monkeypatch):
    _route(
        monkeypatch,
        _resp(200, {"settings": {"alternative_hosts": "A@x.org; b@x.org,a@X.org "}}),
        _resp(200, {"panelists": [{"email": "B@x.org"}, {"email": "c@x.org"}, {"name": "no email"}]}),
    )
    assert hosts.get_webinar_cohost_emails("123") == ["a@x.org", "b@x.org", "c@x.org"]


def test_follows_panelist_pagination(monkeypatch):
    calls = _route(
        monkeypatch,
        _resp(200, {}),
        _resp(200, {"panelists": [{"email": "a@x.org"}], "next_page_token": "n1"}),
        _resp(200, {"panelists": [{"email": "b@x.org"}]}),
    )
    assert hosts.get_webinar_cohost_emails("123") == ["a@x.org", "b@x.org"]
    assert calls[-1]["params"]["next_page_token"] == "n1"


def test_missing_fields_tolerated_and_empty_warns(monkeypatch, caplog):
    _route(monkeypatch, _resp(200, {}), _resp(200, {}))
    with caplog.at_level(logging.WARNING):
        assert hosts.get_webinar_cohost_emails("123") == []
    assert "no co-hosts" in caplog.text


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_non_2xx_returns_none(monkeypatch, status):
    _route(monkeypatch, _resp(status, {"message": "nope"}))
    assert hosts.get_webinar_cohost_emails("123") is None


def test_panelist_failure_returns_none(monkeypatch):
    _route(monkeypatch, _resp(200, {}), _resp(403, {"message": "scope"}))
    assert hosts.get_webinar_cohost_emails("123") is None


def test_404_logs_missing_and_raises_not_found(monkeypatch, caplog):
    _route(monkeypatch, _resp(404, {"message": "gone"}))
    with caplog.at_level(logging.WARNING), pytest.raises(hosts.ZoomWebinarNotFound):
        hosts.get_webinar_cohost_emails("123")
    assert "zoom_webinar_missing" in caplog.text


def test_timeout_returns_none(monkeypatch):
    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(hosts.httpx, "get", boom)
    assert hosts.get_webinar_cohost_emails("123") is None


def test_not_configured_or_blank_id_returns_none(monkeypatch):
    monkeypatch.setattr(hosts, "is_configured", lambda: False)
    assert hosts.get_webinar_cohost_emails("123") is None
    monkeypatch.setattr(hosts, "is_configured", lambda: True)
    assert hosts.get_webinar_cohost_emails("") is None
    assert hosts.get_webinar_cohost_emails(None) is None
