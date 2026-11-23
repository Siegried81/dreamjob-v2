"""The crawler identifies itself with a product token, and a contact only if one is set."""

from __future__ import annotations

import pytest
from dreamjob.config import get_settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DREAMJOB_USER_AGENT", "DreamJobBot/0.1")
    monkeypatch.setenv("DREAMJOB_CRAWLER_CONTACT", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_no_contact_is_built_in():
    assert get_settings().user_agent_header == "DreamJobBot/0.1"


def test_a_configured_contact_is_appended_and_robots_still_match_the_token(monkeypatch):
    monkeypatch.setenv("DREAMJOB_CRAWLER_CONTACT", "ops@example.org")
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.user_agent_header == "DreamJobBot/0.1 (contact: ops@example.org)"
    assert settings.user_agent == "DreamJobBot/0.1", "robots.txt is matched on the token alone"


def test_the_header_the_egress_layer_sends_carries_the_contact(monkeypatch):
    import asyncio

    from dreamjob.egress.client import EgressClient

    monkeypatch.setenv("DREAMJOB_CRAWLER_CONTACT", "https://example.org/bot")
    get_settings.cache_clear()

    async def header() -> str:
        async with EgressClient() as client:
            return client._client_or_raise().headers["User-Agent"]

    assert asyncio.run(header()).endswith("(contact: https://example.org/bot)")
