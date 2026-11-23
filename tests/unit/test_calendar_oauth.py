"""Connecting a Google Calendar end to end, with Google mocked (FR-423, NFR-204).

``/calendar/authorize`` used to hand out a consent URL whose redirect pointed at
a route that did not exist, and ``exchange_code`` was never called, so no
calendar could be connected.  The flow now mirrors the Gmail one: a single-use
``state`` row binds the callback to the job seeker, and the callback needs no
session because the browser arrives from Google.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from dreamjob.config import get_settings
from dreamjob.egress.client import EgressClient, FetchResult

REDIRECT = "http://127.0.0.1:8000/api/pipeline/calendar/callback"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "cal.db"))
    monkeypatch.setenv(
        "DREAMJOB_MASTER_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("DREAMJOB_SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("DREAMJOB_ENV", "development")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "cal-client")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "cal-secret")
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_URI", REDIRECT)
    get_settings.cache_clear()
    from dreamjob.db.migrator import migrate

    migrate()
    yield
    get_settings.cache_clear()


class Google:
    """Token endpoint plus the primary calendar lookup, recording every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.token_status = 200

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        if url.endswith("/token"):
            body = {
                "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/calendar.events",
            }
        else:
            body = {"id": "seeker@gmail.example"}
        text = json.dumps(body)
        status = self.token_status if url.endswith("/token") else 200
        return FetchResult(
            url=url, status_code=status, text=text, content=text.encode(), headers={},
            from_cache=False, raw_document_id=None, content_hash="",
        )


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    fake = Google()
    monkeypatch.setattr(EgressClient, "fetch", lambda self, url, **kw: fake.fetch(url, **kw))
    return fake


@pytest.fixture
def seeker_id() -> str:
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    return insert_row(
        "job_seeker",
        {"email": "seeker@gmail.example", "display_name": "Seeker", "locale": "en",
         "created_at": now, "updated_at": now},
    )


@pytest.fixture
def client(seeker_id):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import pipeline as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/pipeline")
    seeker = CurrentSeeker(
        id=seeker_id, email="seeker@gmail.example", display_name="Seeker", is_admin=False,
        locale="en",
    )
    app.dependency_overrides[current_seeker] = lambda: seeker
    with TestClient(app) as c:
        yield c


def _authorize(client) -> dict:
    response = client.post("/api/pipeline/calendar/authorize", json={"provider": "google"})
    assert response.status_code == 200, response.text
    return response.json()


def test_authorize_binds_a_state_to_the_seeker_and_the_configured_redirect(client, seeker_id):
    from dreamjob.db.connection import query_one

    started = _authorize(client)
    query = parse_qs(urlparse(started["authorization_url"]).query)

    assert query["redirect_uri"] == [REDIRECT]
    assert query["state"] == [started["state"]]
    row = query_one("SELECT * FROM mail_oauth_state WHERE state = ?", (started["state"],))
    assert row["job_seeker_id"] == seeker_id
    assert row["backend"] == "google_calendar"


def test_the_callback_stores_the_calendar_and_the_state_is_single_use(
    client, google, seeker_id
):
    from dreamjob.db.connection import query_all

    started = _authorize(client)
    response = client.get(
        "/api/pipeline/calendar/callback", params={"code": "the-code", "state": started["state"]}
    )

    assert response.status_code == 200, response.text
    token_call = next(c for c in google.calls if c["url"].endswith("/token"))
    assert token_call["data"]["code"] == "the-code"
    assert token_call["data"]["redirect_uri"] == REDIRECT
    accounts = query_all("SELECT * FROM calendar_account WHERE job_seeker_id = ?", (seeker_id,))
    assert [a["address"] for a in accounts] == ["seeker@gmail.example"]
    assert accounts[0]["provider"] == "google"
    assert b"rt-1" not in bytes(accounts[0]["credentials_enc"]), "stored encrypted"

    again = client.get(
        "/api/pipeline/calendar/callback", params={"code": "the-code", "state": started["state"]}
    )
    assert again.status_code == 400


def test_a_provider_error_or_a_foreign_state_connects_nothing(client, google, seeker_id):
    from dreamjob.db.connection import query_all
    from dreamjob.db.repositories import dispatch as dispatch_repo

    refused = client.get(
        "/api/pipeline/calendar/callback", params={"error": "access_denied", "state": "x"}
    )
    assert refused.status_code == 400

    dispatch_repo.create_oauth_state(
        "gmail-state", job_seeker_id=seeker_id, backend="gmail_oauth", code_verifier="v",
        redirect_uri=REDIRECT, scopes="x",
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec="seconds"),
    )
    foreign = client.get(
        "/api/pipeline/calendar/callback", params={"code": "c", "state": "gmail-state"}
    )
    assert foreign.status_code == 400
    assert google.calls == []
    assert query_all("SELECT 1 FROM calendar_account") == []


def test_a_failed_token_exchange_is_reported_not_stored(client, google):
    from dreamjob.db.connection import query_all

    started = _authorize(client)
    google.token_status = 400
    response = client.get(
        "/api/pipeline/calendar/callback", params={"code": "bad", "state": started["state"]}
    )

    assert response.status_code == 400
    assert query_all("SELECT 1 FROM calendar_account") == []
