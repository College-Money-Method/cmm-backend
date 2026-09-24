"""No test may talk to SES.

Learned the hard way: a publish-time ops alert was added to ``publish_service``,
and the ten tests that publish a job promptly mailed a real inbox describing a
webinar that does not exist, on a Vimeo video that does not exist. The suite
loads ``.env``, so it has live AWS credentials, and the ops alert address is on
the sandbox domain, so ``email_sandbox_mode`` waves it through — neither of the
two guards inside ``send_email`` is aimed at this.

The seam is ``_create_ses_client``, which every send resolves at call time and
which the email tests already patch by hand. Replacing it here means a caller
that starts sending mail cannot reach the network by forgetting to patch
something: the send still runs, still writes its ``email_send_log`` row, and
stops at the edge. A test that wants to assert on the SES call patches this same
name itself, and that patch wins for its duration.
"""

from __future__ import annotations

import pytest

from src.emails import ses_client


class _OfflineSesClient:
    """Answers like SES, reaches nothing. Records calls for anyone who looks."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_raw_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "offline-test-message-id"}


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    client = _OfflineSesClient()
    # The real factory is lru_cached, so a client built while some other patch
    # was in force would otherwise outlive it. Cleared on the way in, where the
    # name still refers to the cached original — this fixture's teardown runs
    # before monkeypatch's, so by then the name is the stub and has no cache.
    ses_client._create_ses_client.cache_clear()
    monkeypatch.setattr(ses_client, "_create_ses_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _no_operator_overrides(monkeypatch):
    """Global-settings overrides read the app's own database. Tests must not.

    ``operator_settings`` opens a session through ``get_session_factory``, which
    in a test process points at whatever ``.env`` configures — so leaving it
    live would have every audit-folder lookup dial a real database, and cache
    the answer for five minutes across unrelated tests. Reporting "no override"
    is the same answer an empty config row gives, so the env seed applies and
    the tests that set ``settings.vimeo_audit_folder_uri`` mean what they say.

    A test that wants the real lookup patches ``get_session_factory`` at its own
    session and calls the function directly.
    """
    from src.app_config import operator_settings

    operator_settings.reset_vimeo_folder_cache()
    for lookup in ("vimeo_audit_folder_uri", "vimeo_replay_folder_uri", "vimeo_reel_folder_uri"):
        monkeypatch.setattr(operator_settings, lookup, lambda: None)
