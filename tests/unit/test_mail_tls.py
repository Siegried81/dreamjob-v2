"""Every IMAP connection verifies the server's certificate and host name.

imaplib's default context checks neither, so the Yahoo app password (job
alerts) and the Gmail token (reply polling) went to whoever answered. The
connection class is replaced by a recorder: no socket is opened.
"""
import ssl

import pytest
from dreamjob.mail import inbox, job_alerts, tls


class _Recorder:
    """Stands in for imaplib.IMAP4_SSL and keeps the arguments it was built with."""

    calls: list[dict] = []

    def __init__(self, host, port, *, ssl_context=None, timeout=None):
        _Recorder.calls.append({"host": host, "ssl_context": ssl_context, "timeout": timeout})
        raise ConnectionAbortedError("recorded; no network in tests")


class _ByName:
    """A backend the poller connects by host name, as it does the operator's Gmail."""

    def imap_endpoint(self):
        return "imap.gmail.com", 993


def _assert_verified(call):
    context = call["ssl_context"]
    assert isinstance(context, ssl.SSLContext), "no context passed: imaplib would not verify"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert call["timeout"] == tls.MAIL_SOCKET_TIMEOUT


def test_the_shared_context_verifies():
    context = tls.verified_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_the_job_alert_mailbox_connects_with_a_verifying_context(monkeypatch):
    _Recorder.calls = []
    monkeypatch.setattr(job_alerts.imaplib, "IMAP4_SSL", _Recorder)
    with pytest.raises(ConnectionAbortedError):
        with job_alerts.ImapAlertMailbox("imap.mail.yahoo.com", "user", "app-password"):
            pass
    _assert_verified(_Recorder.calls[0])


def test_the_reply_poller_connects_with_a_verifying_context(monkeypatch):
    _Recorder.calls = []
    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", _Recorder)
    monkeypatch.setattr(inbox.repo, "get_poll_state", lambda _id: {})
    with pytest.raises(ConnectionAbortedError):
        inbox.poll_imap({"id": "acct", "address": "a@example.org"}, _ByName(), password="x")
    _assert_verified(_Recorder.calls[0])
