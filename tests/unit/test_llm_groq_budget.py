"""Groq's gpt-oss models spend the output budget on reasoning (mocked HTTP only).

``classify.employer_kind`` has a 900-token budget and a short JSON answer.  On a
reasoning model the thinking counts against it, so the call came back as
``400 json_validate_failed`` with an empty ``failed_generation``, which the client
treated as final: no verdict for that employer.  Cheap calls now ask for low
reasoning effort, a budget failure is retried once with more room, and the
failure log says what the provider said.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_llm_call_log import _Response, db  # noqa: F401 - the db fixture, used by name
from test_llm_key_rotation import _client

ANSWER = {"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}
GROQ = "https://api.groq.com/openai/v1"
BODY = {"error": {"message": "Failed to generate JSON.", "type": "invalid_request_error",
                  "code": "json_validate_failed", "failed_generation": ""}}
MODELS = {
    "deepseek_base_url": GROQ, "llm_model_cheap": "openai/gpt-oss-20b",
    "llm_model_strong": "openai/gpt-oss-120b", "fallback_llm_api_key": "",
}


class _Bad(_Response):
    def __init__(self, status: int = 400, body: dict | None = None, text: str = "") -> None:
        super().__init__(body if body is not None else BODY)
        self.status_code = status
        self.text = text or str(body if body is not None else BODY)

    def raise_for_status(self) -> None:
        raise RuntimeError(f"HTTP {self.status_code}")


def _scripted(replies: list[_Response], sent: list[dict]):
    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            sent.append(dict(json))
            return replies[min(len(sent) - 1, len(replies) - 1)]

    return _Client


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import dreamjob.llm.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    client_mod.clear_response_cache()
    yield
    client_mod.clear_response_cache()


def test_cheap_calls_to_groq_gpt_oss_ask_for_low_reasoning_and_strong_calls_do_not():
    from dreamjob.llm.client import reasoning_switch

    assert reasoning_switch(GROQ, "openai/gpt-oss-20b", strong=False) == "low"
    assert reasoning_switch(GROQ, "openai/gpt-oss-120b", strong=True) is None
    assert reasoning_switch(GROQ, "llama-3.3-70b-versatile", strong=False) is None
    assert reasoning_switch("https://api.deepseek.com", "deepseek-flash", strong=False) is None


@pytest.mark.usefixtures("db")
def test_the_request_carries_reasoning_effort_only_where_it_is_accepted(monkeypatch):
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _scripted([_Response(ANSWER)], sent))

    _client(**MODELS).complete("classify.employer_kind", "s", "u", prefer_strong=False)

    assert sent[0]["reasoning_effort"] == "low"


@pytest.mark.usefixtures("db")
def test_a_json_validate_failed_400_is_retried_once_with_a_bigger_budget(monkeypatch):
    import dreamjob.llm.client as client_mod

    sent: list[dict] = []
    monkeypatch.setattr(
        client_mod.httpx, "Client", _scripted([_Bad(), _Response(ANSWER)], sent)
    )

    result = _client(**MODELS).complete(
        "classify.employer_kind", "s", "u", prefer_strong=False, max_tokens=900,
        json_mode=True, retries=0,
    )

    assert result.text == '{"ok": true}'
    assert [m["max_tokens"] for m in sent] == [900, 1800]


@pytest.mark.usefixtures("db")
def test_other_400s_are_still_final(monkeypatch):
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError

    sent: list[dict] = []
    other = {"error": {"message": "bad field", "code": "invalid_request"}}
    monkeypatch.setattr(client_mod.httpx, "Client", _scripted([_Bad(body=other)], sent))

    with pytest.raises(LLMError):
        _client(**MODELS).complete("classify.employer_kind", "s", "u", retries=0)

    assert len(sent) == 1


@pytest.mark.usefixtures("db")
def test_a_failure_log_names_the_providers_code_and_a_slice_of_the_body(monkeypatch, caplog):
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError

    monkeypatch.setattr(client_mod.httpx, "Client", _scripted([_Bad(), _Bad()], []))

    with caplog.at_level(logging.WARNING, logger="dreamjob.llm.client"):
        with pytest.raises(LLMError, match="json_validate_failed"):
            _client(**MODELS).complete(
                "classify.employer_kind", "s", "u", json_mode=True, retries=0
            )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "code=json_validate_failed" in text
    assert "Failed to generate JSON" in text
    assert "Bearer" not in text and "k1" not in text, "never the key or the headers"
