"""Microsoft 365 mail over Graph: connect, poll, and never write (FR-326, NFR-204).

Graph and the Microsoft identity platform are replaced by an ``httpx``
MockTransport that records every request, so the tests can assert the one
property that matters most here: the mailbox is only ever *read*.  Every call
to graph.microsoft.com must be a ``GET``; a ``PATCH`` (mark read), a ``POST
/move`` or a ``DELETE`` would fail the suite.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from dreamjob.config import get_settings

_ENV = {
    "M365_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
    "M365_CLIENT_SECRET": "",
    "M365_TENANT": "common",
    "RESEND_API_KEY": "",
    "GMAIL_CLIENT_ID": "",
    "GMAIL_CLIENT_SECRET": "",
    "DREAMJOB_MAIL_BACKEND": "resend",
}

REPLY = (
    b"From: Hiring Manager <hm@northwind.example>\r\n"
    b"To: seeker@contoso.example\r\n"
    b"Subject: Re: Manufacturing Engineer\r\n"
    b"Date: Mon, 12 Oct 2026 09:00:00 +0000\r\n"
    b"Message-ID: <reply-1@northwind.example>\r\n"
    b"In-Reply-To: <unknown@dreamjob.local>\r\n"
    b"\r\n"
    b"Thanks, let us talk on Thursday.\r\n"
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "m365.db"))
    monkeypatch.setenv(
        "DREAMJOB_MASTER_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("DREAMJOB_SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("DREAMJOB_ENV", "development")
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from dreamjob.db.migrator import migrate

    migrate()
    yield
    get_settings.cache_clear()


class Graph:
    """A fake Microsoft: token endpoint, /me, an inbox of one message."""

    def __init__(self, *, scope: str | None = None, messages: dict[str, bytes] | None = None):
        self.requests: list[httpx.Request] = []
        self.scope = scope or "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access"
        self.messages = messages if messages is not None else {"AAMk-1": REPLY}
        self.forbid_graph = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "login.microsoftonline.com" and url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": f"at-{len(self.requests)}",
                    "refresh_token": f"rt-{len(self.requests)}",
                    "expires_in": 3600,
                    "scope": self.scope,
                    "token_type": "Bearer",
                },
            )
        if url.host == "graph.microsoft.com" and self.forbid_graph:
            return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
        if url.host == "graph.microsoft.com":
            if url.path == "/v1.0/me":
                return httpx.Response(200, json={"mail": "Seeker@Contoso.example"})
            if url.path == "/v1.0/me/mailFolders/inbox/messages":
                return httpx.Response(
                    200,
                    json={"value": [{"id": i, "receivedDateTime": "2026-10-12T09:00:00Z"}
                                    for i in self.messages]},
                )
            if url.path.startswith("/v1.0/me/messages/") and url.path.endswith("/$value"):
                message_id = url.path.split("/")[-2]
                return httpx.Response(200, content=self.messages[message_id])
        return httpx.Response(404, json={"error": "unexpected"})

    def graph_methods(self) -> set[str]:
        return {r.method for r in self.requests if r.url.host == "graph.microsoft.com"}

    def token_forms(self) -> list[dict[str, list[str]]]:
        return [
            parse_qs(r.content.decode())
            for r in self.requests
            if r.url.host == "login.microsoftonline.com"
        ]


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Graph:
    from dreamjob.mail import m365

    fake = Graph()
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(m365.httpx, "Client", client)
    return fake


@pytest.fixture
def seeker_id() -> str:
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    return insert_row(
        "job_seeker",
        {"email": "seeker@contoso.example", "display_name": "Seeker", "locale": "en",
         "created_at": now, "updated_at": now},
    )


def _connect(seeker_id: str) -> dict:
    from dreamjob.mail import m365

    started = m365.authorization_url(seeker_id)
    return m365.complete_authorization(started["state"], "the-code")


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def test_without_an_app_registration_it_says_what_to_create(monkeypatch, seeker_id):
    from dreamjob.mail import m365
    from dreamjob.mail.base import MailBackendNotConfigured

    monkeypatch.setenv("M365_CLIENT_ID", "")
    get_settings.cache_clear()

    with pytest.raises(MailBackendNotConfigured, match="M365_CLIENT_ID"):
        m365.authorization_url(seeker_id)


def test_the_authorisation_url_asks_for_reading_only(seeker_id):
    from dreamjob.mail import m365

    started = m365.authorization_url(seeker_id, login_hint="seeker@contoso.example")
    query = parse_qs(urlparse(started["authorization_url"]).query)

    assert urlparse(started["authorization_url"]).netloc == "login.microsoftonline.com"
    scopes = query["scope"][0].split()
    assert "https://graph.microsoft.com/Mail.Read" in scopes
    assert not [s for s in scopes if "ReadWrite" in s or "Send" in s]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://localhost:8000/api/mail/m365/callback"]


def test_connecting_stores_an_encrypted_read_only_mailbox(graph, seeker_id):
    from dreamjob.db.repositories import dispatch as repo

    account = _connect(seeker_id)

    assert account["backend"] == "m365_graph"
    assert account["address"] == "seeker@contoso.example"
    row = repo.get_account(account["id"], seeker_id)
    assert b"rt-" not in bytes(row["credentials_enc"]), "the refresh token is sealed at rest"
    form = graph.token_forms()[0]
    assert form["grant_type"] == ["authorization_code"]
    assert "code_verifier" in form
    assert "client_secret" not in form, "a public client sends no secret"


def test_a_token_that_could_write_is_refused(monkeypatch, seeker_id):
    from dreamjob.mail import m365

    wide = Graph(scope="https://graph.microsoft.com/Mail.ReadWrite offline_access")
    real_client = httpx.Client
    monkeypatch.setattr(
        m365.httpx, "Client",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(wide)}),
    )

    with pytest.raises(m365.M365AuthError, match="mail.readwrite"):
        _connect(seeker_id)


def test_a_state_minted_for_gmail_is_not_accepted(graph, seeker_id):
    from datetime import UTC, datetime, timedelta

    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import m365

    repo.create_oauth_state(
        "gmail-state", job_seeker_id=seeker_id, backend="gmail_oauth", code_verifier="v",
        redirect_uri="http://127.0.0.1:8000/api/mail/gmail/callback", scopes="x",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec="seconds"),
    )

    with pytest.raises(m365.M365AuthError):
        m365.complete_authorization("gmail-state", "the-code")


# ---------------------------------------------------------------------------
# Polling: read, and only read
# ---------------------------------------------------------------------------


def test_polling_reads_the_inbox_and_never_writes_to_it(graph, seeker_id):
    from dreamjob.db.connection import query_all
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)

    result = inbox.poll_account(account)

    assert result["transport"] == "graph_api"
    assert result["polled"] == 1
    assert graph.graph_methods() == {"GET"}, "no PATCH, POST or DELETE ever reaches the mailbox"
    listing = next(r for r in graph.requests if r.url.path.endswith("/inbox/messages"))
    assert "receivedDateTime ge" in listing.url.params["$filter"]
    replies = query_all("SELECT from_address FROM incoming_reply WHERE job_seeker_id = ?",
                        (seeker_id,))
    assert [r["from_address"] for r in replies] == ["hm@northwind.example"]


def test_poll_all_includes_the_microsoft_mailbox(graph, seeker_id):
    from dreamjob.mail import inbox
    from dreamjob.mail.base import pollable_backends

    _connect(seeker_id)

    assert "m365_graph" in pollable_backends()
    assert [r["transport"] for r in inbox.poll_all(seeker_id)] == ["graph_api"]


def test_an_expired_token_is_refreshed_and_the_rotated_one_kept(graph, seeker_id):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import m365

    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)
    backend = m365.M365GraphBackend(account)
    backend.credentials["access_token_expires_at"] = (
        datetime.now(UTC) - timedelta(minutes=1)
    ).isoformat()

    token = backend.access_token()

    refresh = graph.token_forms()[-1]
    assert refresh["grant_type"] == ["refresh_token"]
    stored = m365._decrypt(repo.get_account(account["id"], seeker_id)["credentials_enc"],
                           seeker_id)
    assert stored["access_token"] == token
    assert stored["refresh_token"] != "rt-1", "the rotated refresh token replaces the old one"


def test_a_refused_read_asks_for_a_reconnection(graph, seeker_id):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)
    graph.forbid_graph = True

    result = inbox.poll_account(account)

    assert result["polled"] == 0
    assert "reconnect" in result["error"]


# ---------------------------------------------------------------------------
# Never a sender
# ---------------------------------------------------------------------------


def test_the_backend_refuses_to_send(graph, seeker_id):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import m365
    from dreamjob.mail.base import MailBackendError, OutgoingMessage

    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)

    with pytest.raises(MailBackendError, match="read-only"):
        m365.M365GraphBackend(account).send(
            OutgoingMessage(to_email="hm@northwind.example", subject="Hi", body_text="Hello")
        )
    assert m365.M365GraphBackend.capabilities.can_send is False


def test_the_dispatcher_never_sends_from_a_read_only_mailbox(graph, seeker_id):
    from dreamjob.mail import dispatcher
    from dreamjob.mail.base import MailBackendError

    _connect(seeker_id)

    backend, account = dispatcher.resolve_backend(seeker_id)
    assert account["backend"] == "resend", "the relay, never the read-only mailbox"
    with pytest.raises(MailBackendError, match="read-only"):
        dispatcher.resolve_backend(seeker_id, "m365_graph")


def test_status_reports_setup_then_connection(graph, seeker_id, monkeypatch):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import m365

    assert m365.M365GraphBackend(None).status()["configured"] is False
    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)
    status = m365.M365GraphBackend(account).status()
    assert status["configured"] is True
    assert status["read_only"] is True
    monkeypatch.setenv("M365_CLIENT_ID", "")
    get_settings.cache_clear()
    assert "M365_CLIENT_ID" in m365.M365GraphBackend(account).status()["reason"]


def test_disconnecting_wipes_the_credential_and_says_where_to_finish(graph, seeker_id):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import m365

    account = repo.get_account(_connect(seeker_id)["id"], seeker_id)

    result = m365.disconnect(account)

    row = repo.get_account(account["id"], seeker_id)
    assert row["credentials_enc"] is None and row["is_active"] == 0
    assert "account.live.com" in json.dumps(result["finish_at"])


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(seeker_id):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import mail as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/mail")
    seeker = CurrentSeeker(id=seeker_id, email="seeker@contoso.example", display_name="S",
                           is_admin=False, locale="en")
    app.dependency_overrides[current_seeker] = lambda: seeker
    with TestClient(app) as c:
        yield c


def test_the_api_connects_through_the_callback(client, graph):
    started = client.get("/api/mail/m365/authorize").json()

    page = client.get("/api/mail/m365/callback", params={"state": started["state"], "code": "c"})

    assert page.status_code == 200
    assert "read-only" in page.text
    backends = {b["backend"]: b for b in client.get("/api/mail/status").json()["backends"]}
    assert backends["m365_graph"]["configured"] is True


def test_a_denied_consent_is_shown_escaped(client):
    page = client.get(
        "/api/mail/m365/callback",
        params={"error": "access_denied", "error_description": "<script>x</script>"},
    )

    assert page.status_code == 400
    assert "<script>" not in page.text


def test_revoke_through_the_api_uses_the_microsoft_path(client, graph, seeker_id):
    account = _connect(seeker_id)

    body = client.post(f"/api/mail/accounts/{account['id']}/revoke").json()

    assert body["revoked_locally"] is True
    assert body["revoked_at_provider"] is False
