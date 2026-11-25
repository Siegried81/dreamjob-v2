"""Groq's two size and rate limits, as the client meets them (mocked HTTP only).

What is pinned down here:

* the wait a 429 asks for is read from ``retry-after`` or from Groq's
  "try again in 33.78s" message, and honoured up to ``_RATE_LIMIT_WAIT_MAX``
  once every key has been refused;
* a 413 is final for that endpoint: not retried, not sent with another key,
  raised as ``RequestTooLarge`` so the caller can send less;
* the call log stores the provider's status, code and body, not only httpx's
  one-line message, which said "429 Too Many Requests" and nothing else.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_llm_call_log import _Response, db  # noqa: F401 - the db fixture, used by name
from test_llm_groq_budget import ANSWER, MODELS, _Bad
from test_llm_key_rotation import _client

RATE_BODY = {"error": {
    "message": "Rate limit reached for model `openai/gpt-oss-20b` on tokens per minute "
               "(TPM): Limit 8000, Used 5465, Requested 7039. Please try again in 33.78s.",
    "type": "tokens", "code": "rate_limit_exceeded",
}}
SIZE_BODY = {"error": {
    "message": "Request too large for model `openai/gpt-oss-20b` on tokens per minute "
               "(TPM): Limit 8000, Requested 8669, please reduce your message size.",
    "type": "tokens", "code": "rate_limit_exceeded",
}}


class _Status(_Bad):
    def __init__(self, status: int, body: dict, headers: dict | None = None) -> None:
        super().__init__(status=status, body=body, text=body["error"]["message"])
        self.headers = headers or {}


def _per_call(replies: list[_Response], seen: list[str]):
    """An ``httpx.Client`` stand-in answering from ``replies`` in order, noting the key."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            seen.append(headers["Authorization"].removeprefix("Bearer "))
            return replies[min(len(seen) - 1, len(replies) - 1)]

    return _Client


@pytest.fixture(autouse=True)
def _clean_cache():
    import dreamjob.llm.client as client_mod

    client_mod.clear_response_cache()
    yield
    client_mod.clear_response_cache()


@pytest.mark.parametrize(
    ("headers", "text", "expected"),
    [
        ({"retry-after": "12"}, "", 12.0),
        ({}, "Please try again in 33.78s.", 33.78),
        ({}, "Please try again in 1m2.5s.", 62.5),
        ({}, "Please try again in 450ms.", 0.45),
        ({}, "Rate limit reached.", None),
    ],
)
def test_the_wait_is_read_from_the_header_or_the_message(headers, text, expected) -> None:
    from dreamjob.llm.client import retry_after_seconds

    resp = _Status(429, {"error": {"message": text}}, headers)
    assert retry_after_seconds(resp) == expected


@pytest.mark.usefixtures("db")
def test_every_key_rate_limited_waits_for_the_window_groq_names(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    slept: list[float] = []
    limited = _Status(429, RATE_BODY)
    monkeypatch.setattr(
        client_mod.httpx, "Client", _per_call([limited, limited, _Response(ANSWER)], seen)
    )
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    result = _client(**MODELS, deepseek_api_key_2="k2").complete(
        "classify.employer_kind", "s", "u", retries=2
    )

    assert result.text == '{"ok": true}'
    assert seen == ["k1", "k2", "k1"]
    assert slept == [33.78], "the 2 s backoff alone retried inside Groq's window"


@pytest.mark.usefixtures("db")
def test_the_wait_is_capped(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod

    slept: list[float] = []
    limited = _Status(429, RATE_BODY, {"retry-after": "900"})
    monkeypatch.setattr(client_mod.httpx, "Client", _per_call([limited, _Response(ANSWER)], []))
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    _client(**MODELS).complete("classify.employer_kind", "s", "u", retries=1)

    assert slept == [client_mod._RATE_LIMIT_WAIT_MAX]


@pytest.mark.usefixtures("db")
def test_a_413_is_raised_at_once_as_request_too_large(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import RequestTooLarge

    seen: list[str] = []
    slept: list[float] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_call([_Status(413, SIZE_BODY)], seen))
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    with pytest.raises(RequestTooLarge, match="Requested 8669"):
        _client(**MODELS, deepseek_api_key_2="k2").complete(
            "classify.employer_kind", "s", "u", retries=2
        )

    assert seen == ["k1"], "another key or another try gets the same refusal"
    assert slept == []


@pytest.mark.usefixtures("db")
def test_the_call_log_keeps_the_providers_status_code_and_message(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod
    from dreamjob.db.connection import query_one
    from dreamjob.llm.client import LLMError

    monkeypatch.setattr(client_mod.httpx, "Client", _per_call([_Status(429, RATE_BODY)], []))
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    with pytest.raises(LLMError):
        _client(**MODELS).complete("classify.employer_kind", "s", "u", retries=0)

    row = query_one("SELECT status, error FROM llm_call ORDER BY created_at DESC, id DESC LIMIT 1")
    assert row["status"] == "error"
    assert "status=429" in row["error"]
    assert "code=rate_limit_exceeded" in row["error"]
    assert "try again in 33.78s" in row["error"]
    assert "k1" not in row["error"] and "Bearer" not in row["error"]
