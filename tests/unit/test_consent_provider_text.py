"""The CR-410 consent names the model API the configuration actually uses.

The text used to say "DeepSeek" whatever the endpoint was, so a deployment
pointed at Groq asked the job seeker to agree to a transfer that was not the
one taking place.
"""

from __future__ import annotations

import pytest
from dreamjob.config import get_settings
from dreamjob.security import auth_service as auth


@pytest.fixture
def llm_env(monkeypatch):
    """Set the LLM endpoints for one test and rebuild the cached settings."""

    def apply(base_url: str, fallback_key: str = "", fallback_url: str = "https://api.deepseek.com"):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", base_url)
        monkeypatch.setenv("DREAMJOB_FALLBACK_LLM_API_KEY", fallback_key)
        monkeypatch.setenv("DREAMJOB_FALLBACK_LLM_BASE_URL", fallback_url)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_a_groq_endpoint_is_named_groq(llm_env):
    llm_env("https://api.groq.com/openai/v1")
    text = auth.consent_text("llm_transfer")
    assert "the Groq API (api.groq.com)" in text
    assert "DeepSeek" not in text


def test_the_default_endpoint_is_named_deepseek(llm_env):
    llm_env("https://api.deepseek.com")
    assert "the DeepSeek API (api.deepseek.com)" in auth.consent_text("llm_transfer")


def test_a_fallback_with_a_key_is_named_too(llm_env):
    llm_env("https://api.groq.com/openai/v1", fallback_key="sk-test")
    text = auth.consent_text("llm_transfer")
    assert "the Groq API (api.groq.com) (and, when that fails, the DeepSeek API" in text


def test_a_fallback_without_a_key_is_not_named(llm_env):
    """With no key the fallback is never called, so no data can reach it."""
    llm_env("https://api.groq.com/openai/v1", fallback_key="")
    assert "DeepSeek" not in auth.consent_text("llm_transfer")


def test_an_unknown_host_is_shown_as_is(llm_env):
    llm_env("https://llm.example.org/v1")
    assert "the API at llm.example.org" in auth.consent_text("llm_transfer")


def test_the_state_and_the_other_kinds_use_the_same_text(llm_env):
    llm_env("https://api.groq.com/openai/v1")
    assert "{providers}" not in auth.consent_text("llm_transfer")
    assert auth.consent_text("enrichment") == auth.CONSENT_KINDS["enrichment"]
    assert str(auth.ConsentRequired("llm_transfer")).count("Groq") == 1
