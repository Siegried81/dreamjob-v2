"""DeepSeek's thinking switch: off for cheap calls, on for strong ones, DeepSeek only.

deepseek-flash serves both roles, so the role travels in the request (the
``thinking`` field) and in the response-cache key instead of in the model name.
No network: ``httpx.Client`` is replaced by a stub that records each request.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_llm_call_log import _Response, db  # noqa: F401 - the db fixture, used by name
from test_llm_key_rotation import _client

ANSWER = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
DEEPSEEK = "https://api.deepseek.com"
GROQ = "https://api.groq.com/openai/v1"
#: Settings read the machine's .env, so the models are pinned here.
FLASH = {"llm_model_cheap": "deepseek-flash", "llm_model_strong": "deepseek-flash",
         "fallback_llm_model_cheap": "deepseek-flash", "fallback_llm_model_strong": "deepseek-flash"}


def _recording(sent: list[dict], fail_hosts: tuple[str, ...] = ()):
    """An ``httpx.Client`` stand-in that keeps every JSON body it is sent."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            sent.append({"url": url, **json})
            if any(h in url for h in fail_hosts):
                resp = _Response({})
                resp.status_code = 503

                def _raise() -> None:
                    raise RuntimeError("503")

                resp.raise_for_status = _raise
                return resp
            return _Response(ANSWER)

    return _Client


@pytest.fixture(autouse=True)
def _no_sleep_no_cache(monkeypatch):
    import dreamjob.llm.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    client_mod.clear_response_cache()
    yield
    client_mod.clear_response_cache()


def test_switch_is_deepseek_only_and_follows_the_role() -> None:
    from dreamjob.llm.client import thinking_switch

    assert thinking_switch(DEEPSEEK, strong=False) == {"type": "disabled"}
    assert thinking_switch(DEEPSEEK + "/", strong=True) == {"type": "enabled"}
    assert thinking_switch(GROQ, strong=True) is None
    assert thinking_switch("", strong=False) is None


@pytest.mark.usefixtures("db")
@pytest.mark.parametrize("prefer_strong,expected", [(False, "disabled"), (True, "enabled")])
def test_deepseek_call_carries_the_role(monkeypatch, prefer_strong, expected) -> None:
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _recording(sent))
    client = _client(deepseek_base_url=DEEPSEEK, **FLASH)

    client.complete("classify.reply", "s", "u", prefer_strong=prefer_strong, retries=0)

    assert sent[0]["model"] == "deepseek-flash"
    assert sent[0]["thinking"] == {"type": expected}


@pytest.mark.usefixtures("db")
def test_unknown_task_defaults_to_strong_and_thinks(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _recording(sent))

    _client(deepseek_base_url=DEEPSEEK, **FLASH).complete("some.new_task", "s", "u", retries=0)

    assert sent[0]["thinking"] == {"type": "enabled"}


@pytest.mark.usefixtures("db")
def test_groq_main_gets_no_field_but_deepseek_fallback_does(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _recording(sent, fail_hosts=("groq",)))
    client = _client(
        **{**FLASH, "llm_model_cheap": "openai/gpt-oss-20b"},
        deepseek_base_url=GROQ,
        fallback_llm_base_url=DEEPSEEK,
        fallback_llm_api_key="ds",
    )

    result = client.complete("classify.reply", "s", "u", prefer_strong=False, retries=0)

    assert result.model == "deepseek-flash"
    assert "thinking" not in sent[0]
    assert sent[-1]["url"].startswith(DEEPSEEK)
    assert sent[-1]["thinking"] == {"type": "disabled"}


@pytest.mark.usefixtures("db")
def test_strong_retry_is_not_served_the_cached_cheap_answer(monkeypatch) -> None:
    """Same task, prompt and model name: only the role differs, so two calls go out."""
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _recording(sent))
    client = _client(deepseek_base_url=DEEPSEEK, **FLASH)
    assert "classify.employer_kind" in client_mod.CACHEABLE_TASKS

    client.complete("classify.employer_kind", "s", "u", prefer_strong=False, retries=0)
    client.complete("classify.employer_kind", "s", "u", prefer_strong=True, retries=0)
    client.complete("classify.employer_kind", "s", "u", prefer_strong=False, retries=0)

    assert [b["thinking"]["type"] for b in sent] == ["disabled", "enabled"]


@pytest.mark.usefixtures("db")
@pytest.mark.parametrize("prefer_strong,model,thinking", [
    (False, "deepseek-flash", "disabled"),
    (True, "deepseek-v4-pro", "enabled"),
])
def test_fallback_model_follows_the_role(monkeypatch, prefer_strong, model, thinking) -> None:
    """The fallback picks its model from the call's role, not from the main model's name."""
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _recording(sent, fail_hosts=("groq",)))
    client = _client(
        **{**FLASH, "llm_model_cheap": "openai/gpt-oss-20b", "llm_model_strong": "openai/gpt-oss-20b",
           "fallback_llm_model_strong": "deepseek-v4-pro"},
        deepseek_base_url=GROQ,
        fallback_llm_base_url=DEEPSEEK,
        fallback_llm_api_key="ds",
    )

    result = client.complete("classify.reply", "s", "u", prefer_strong=prefer_strong, retries=0)

    assert result.model == model
    assert sent[-1]["thinking"] == {"type": thinking}
