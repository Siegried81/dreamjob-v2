"""The configured search API, used by profile enrichment and kept secret (FR-122, FR-303).

Offline: a stub egress answers by URL, so the tests see exactly which endpoint
each query reached, with which headers and parameters.  Three things are
pinned - enrichment asks the administrator's search API when one is switched
on, falls back to DuckDuckGo when it is off or fails, and the API never hands
the stored key back to the screen that configures it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
)

KEY = "brave-secret-key-123"

DDG_PAGE = """<html><body><div id="links">
<div class="result"><a class="result__a" href="https://example.org/ddg-hit">Hit</a>
<a class="result__snippet">from duckduckgo</a></div></div></body></html>"""

BRAVE_BODY = {"web": {"results": [
    {"url": "https://example.org/brave-hit", "description": "from brave"},
]}}


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()
    from dreamjob.db.migrator import migrate

    migrate()
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


class Result:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class StubEgress:
    """Answers the search API and DuckDuckGo; records every call."""

    def __init__(self, *, api_status: int = 200, api_body: Any = BRAVE_BODY) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.api_status = api_status
        self.api_body = api_body

    async def fetch(self, url: str, **kwargs: Any) -> Result:
        self.calls.append((url, kwargs))
        if "duckduckgo.com" in url:
            return Result(DDG_PAGE)
        return Result(json.dumps(self.api_body), self.api_status)

    def hosts(self) -> list[str]:
        from urllib.parse import urlparse

        return [urlparse(url).netloc for url, _ in self.calls]


def _configure(provider: str = "brave", *, enabled: bool = True, url: str = "") -> None:
    from dreamjob.db.repositories import knowledge as kb
    from dreamjob.pipeline import email_patterns as patterns

    kb.set_setting(patterns.SETTING_SEARCH_ENABLED, enabled)
    kb.set_setting(patterns.SETTING_SEARCH_PROVIDER, provider)
    kb.set_setting(patterns.SETTING_SEARCH_KEY, KEY)
    kb.set_setting(patterns.SETTING_SEARCH_URL, url)


def _search(egress: StubEgress) -> list:
    from dreamjob.pipeline import enrichment

    return asyncio.run(enrichment.web_search(egress, '"Jane Doe" engineer', max_results=5))


# ---------------------------------------------------------------------------
# Enrichment search
# ---------------------------------------------------------------------------


def test_without_a_provider_enrichment_asks_duckduckgo():
    egress = StubEgress()

    hits = _search(egress)

    assert egress.hosts() == ["html.duckduckgo.com"]
    assert [h.url for h in hits] == ["https://example.org/ddg-hit"]


def test_a_configured_provider_is_used_instead_of_duckduckgo():
    _configure("brave")
    egress = StubEgress()

    hits = _search(egress)

    assert egress.hosts() == ["api.search.brave.com"]
    _url, kwargs = egress.calls[0]
    assert kwargs["headers"]["X-Subscription-Token"] == KEY
    assert kwargs["params"]["q"] == '"Jane Doe" engineer'
    assert [(h.url, h.snippet, h.rank) for h in hits] == [
        ("https://example.org/brave-hit", "from brave", 1)
    ]


def test_a_switched_off_provider_is_not_used():
    _configure("brave", enabled=False)
    egress = StubEgress()

    _search(egress)

    assert egress.hosts() == ["html.duckduckgo.com"]


def test_a_failing_provider_falls_back_to_duckduckgo():
    _configure("brave")
    egress = StubEgress(api_status=429)

    hits = _search(egress)

    assert egress.hosts() == ["api.search.brave.com", "html.duckduckgo.com"]
    assert [h.url for h in hits] == ["https://example.org/ddg-hit"]


def test_google_cse_uses_its_configured_endpoint():
    _configure("google_cse", url="https://www.googleapis.com/customsearch/v1?cx=abc&key=k")
    egress = StubEgress(api_body={"items": [{"link": "https://example.org/g", "snippet": "g"}]})

    hits = _search(egress)

    url, kwargs = egress.calls[0]
    assert url.startswith("https://www.googleapis.com/customsearch/v1")
    assert kwargs["params"] == {"q": '"Jane Doe" engineer', "num": 5}
    assert [h.url for h in hits] == ["https://example.org/g"]


def test_the_contacts_search_still_reads_addresses_from_snippets():
    """``search_for_contacts`` now shares ``provider_query``; its answer is unchanged."""
    from dreamjob.pipeline import email_patterns as patterns

    _configure("brave")
    egress = StubEgress(api_body={"web": {"results": [
        {"url": "https://acme.be/blog/post", "description": "write to jobs@acme.be"},
    ]}})

    found = asyncio.run(patterns.search_for_contacts("Acme", "acme.be", egress=egress))

    assert [f.email for f in found] == ["jobs@acme.be"]
    assert all(host == "api.search.brave.com" for host in egress.hosts())


# ---------------------------------------------------------------------------
# The settings API never echoes the key
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import contacts as router_module
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/contacts")
    admin = CurrentSeeker(id="admin-1", email="a@example.test", display_name="A",
                          is_admin=True, locale="en")
    app.dependency_overrides[current_seeker] = lambda: admin
    app.dependency_overrides[current_admin] = lambda: admin
    with TestClient(app) as c:
        yield c


def test_saving_a_key_never_returns_it(client):
    saved = client.put(
        "/api/contacts/search-provider",
        json={"enabled": True, "provider": "brave", "api_key": KEY, "url": ""},
    )
    read = client.get("/api/contacts/search-provider")

    for response in (saved, read):
        assert response.status_code == 200
        assert KEY not in response.text
        assert response.json()["configured"] is True


def test_saving_without_a_key_keeps_the_stored_one(client):
    from dreamjob.pipeline import email_patterns as patterns

    _configure("brave")
    client.put("/api/contacts/search-provider",
               json={"enabled": True, "provider": "bing", "api_key": "", "url": ""})

    assert patterns.search_config()["key"] == KEY
    assert patterns.search_config()["provider"] == "bing"


# ---------------------------------------------------------------------------
# A switch turned off stays off
# ---------------------------------------------------------------------------


def test_a_boolean_setting_reads_back_as_written():
    """``str(False)`` used to be stored, and read back as the truthy ``"False"``."""
    from dreamjob.db.repositories import knowledge as kb

    kb.set_setting("test.flag", False)
    assert kb.get_setting("test.flag") is False
    kb.set_setting("test.flag", True)
    assert kb.get_setting("test.flag") is True


def test_a_flag_stored_the_old_way_is_still_read_correctly():
    from dreamjob.db.connection import execute, utcnow
    from dreamjob.db.repositories import knowledge as kb

    execute(
        "INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?)",
        ("test.legacy", "False", utcnow()),
    )

    assert kb.get_setting("test.legacy") is False


def test_switching_the_search_provider_off_through_the_api_turns_it_off(client):
    from dreamjob.pipeline import email_patterns as patterns

    client.put("/api/contacts/search-provider",
               json={"enabled": True, "provider": "brave", "api_key": KEY, "url": ""})
    assert patterns.search_enabled() is True

    client.put("/api/contacts/search-provider",
               json={"enabled": False, "provider": "brave", "api_key": "", "url": ""})

    assert patterns.search_enabled() is False
    assert client.get("/api/contacts/search-provider").json()["enabled"] is False
