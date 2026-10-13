"""The egress client refuses non-public targets (SSRF guard).

A URL from a job board or a company site - or a redirect such a site sends -
must not make the server fetch cloud metadata (169.254.169.254), localhost or
a LAN host.  Nothing here touches the network: DNS is a stub and the real
:class:`EgressClient` runs on an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import socket
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dreamjob.config import get_settings
from dreamjob.egress.client import BlockedTarget, DomainLimiter, EgressClient
from dreamjob.security.network import is_public_address

PUBLIC_IP = "93.184.216.34"
DNS = {
    "jobs.example.com": PUBLIC_IP,
    "intranet.example.com": "10.0.0.5",
    "localhost": "127.0.0.1",
}

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()

    def fake_getaddrinfo(host, port, *args, **kwargs):  # no real DNS
        if host not in DNS:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (DNS[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _handler(request: httpx.Request) -> httpx.Response:
    """A public site whose /go page redirects to cloud metadata."""
    if request.url.path == "/go":
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    if request.url.path == "/go-local":
        return httpx.Response(302, headers={"location": "http://localhost:8000/admin"})
    return httpx.Response(200, text=f"ok {request.url.host}")


def fetch(url: str, *, seen: list[str] | None = None, **client_kwargs):
    """Fetch ``url`` through a real EgressClient (guard included) on a mock socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return _handler(request)

    async def main():
        client = EgressClient(
            respect_robots=False,
            store_raw=False,
            transport=httpx.MockTransport(handler),
            **client_kwargs,
        )
        async with client:
            client.limiter = DomainLimiter(1000.0)  # no pacing sleeps
            result = await client.fetch(url, use_cache=False, max_retries=1)
            return result, client.stats

    return asyncio.run(main())


def test_public_host_is_fetched() -> None:
    result, _ = fetch("https://jobs.example.com/careers")
    assert result.status_code == 200
    assert result.text == "ok jobs.example.com"


def test_redirect_to_cloud_metadata_is_blocked() -> None:
    seen: list[str] = []
    with pytest.raises(BlockedTarget):
        fetch("https://jobs.example.com/go", seen=seen)
    # The public hop was made; the metadata hop never left the client.
    assert seen == ["https://jobs.example.com/go"]


def test_redirect_to_localhost_is_blocked() -> None:
    seen: list[str] = []
    with pytest.raises(BlockedTarget):
        fetch("https://jobs.example.com/go-local", seen=seen)
    assert seen == ["https://jobs.example.com/go-local"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://intranet.example.com/",  # a name that resolves to RFC 1918
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 smuggling
    ],
)
def test_non_public_initial_url_is_blocked(url: str) -> None:
    seen: list[str] = []
    with pytest.raises(BlockedTarget):
        fetch(url, seen=seen)
    assert seen == []


def test_blocked_fetch_is_counted() -> None:
    async def main():
        client = EgressClient(
            respect_robots=False, store_raw=False, transport=httpx.MockTransport(_handler)
        )
        async with client:
            with pytest.raises(BlockedTarget):
                await client.fetch("http://127.0.0.1/", use_cache=False, max_retries=3)
            return client.stats

    assert asyncio.run(main())["blocked"] == 1


def test_allow_private_is_the_explicit_opt_out() -> None:
    result, _ = fetch("http://127.0.0.1:9222/json/version", allow_private=True)
    assert result.status_code == 200


def test_non_http_scheme_is_blocked() -> None:
    async def main():
        client = EgressClient(transport=httpx.MockTransport(_handler))
        async with client:
            await client._guard_request(httpx.Request("GET", "ftp://jobs.example.com/x"))  # noqa: SLF001

    with pytest.raises(BlockedTarget):
        asyncio.run(main())


@pytest.mark.parametrize(
    ("address", "public"),
    [
        (PUBLIC_IP, True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(address) is public
