"""Host digest renderer: escaping, header safety, banner and undelivered line."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.schools.models import School
from src.workshops.host_digest_email import COHOST_LOOKUP_BANNER, render_host_digest
from src.workshops.models import Webinar, WorkshopRegistration

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)  # 6:00 PM EDT
EXPIRES = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
LINK = "https://app.example.com/workshop-hosts/abc"


def _webinar(name="FAFSA Night") -> Webinar:
    return Webinar(webinar_name=name, start_datetime=START)


def _reg(questions, name="Pat Doe", school="Annie Wright", grade="11") -> WorkshopRegistration:
    return WorkshopRegistration(
        full_name=name, grade=grade, questions=questions, school=School(name=school) if school else None
    )


def _render(webinar=None, regs=(), **kw):
    return render_host_digest(webinar or _webinar(), regs, LINK, EXPIRES, tz=TZ, **kw)


def test_subject_and_body_basics():
    subject, html, text = _render(regs=[_reg("Will aid cover room?"), _reg(None, name="Quiet")])
    assert subject == "Registrant questions: FAFSA Night · Wed, Oct 14, 6:00 PM EDT"
    for body in (html, text):
        assert "2 approved registrant(s), 1 with questions" in body
        assert LINK in body
        assert "Pat Doe · Annie Wright · Grade 11" in body
        assert "Quiet" not in body  # no question, not listed
        assert "works until Wed, Oct 21, 8:00 PM EDT" in body


def test_escapes_name_and_questions_and_keeps_line_breaks():
    subject, html, text = _render(
        webinar=_webinar("<b>A&B</b>"), regs=[_reg("<script>alert(1)</script> & more\nline two")]
    )
    assert "<script>" not in html and "<b>A&B</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more<br>line two" in html
    assert "&lt;b&gt;A&amp;B&lt;/b&gt;" in html
    assert "<script>alert(1)</script> & more\nline two" in text  # plain text is not HTML


def test_subject_strips_crlf():
    subject, _, _ = _render(webinar=_webinar("FAFSA\r\nBcc: evil@x.com"))
    assert "\r" not in subject and "\n" not in subject


def test_no_questions_yet():
    _, html, text = _render(regs=[_reg(None), _reg("   ")])
    assert "No questions yet" in html and "No questions yet" in text


def test_banner_and_undelivered_line():
    _, html, text = _render(cohost_lookup_failed=True, undelivered=[("a@x.com", "suppressed"), ("b@x.com", "failed")])
    for body in (html, text):
        assert "forward this link" in body
        assert "Not delivered to: a@x.com (suppressed), b@x.com (failed)" in body
    assert COHOST_LOOKUP_BANNER in text

    _, html, text = _render()
    assert "forward this link" not in html and "Not delivered" not in text
