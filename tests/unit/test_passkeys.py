"""Passkeys (NFR-202): registration and sign-in against a software authenticator.

The browser and the authenticator are replaced by :class:`SoftAuthenticator`,
which builds exactly the bytes a platform authenticator returns - CBOR
attestation object with ``fmt: none``, authenticator data with the
relying-party hash, flags and counter, and an ECDSA P-256 signature over
``authenticatorData || SHA-256(clientDataJSON)``.  So the real verifier
(py_webauthn) runs on every test; nothing in the cryptographic path is mocked.
No network: WebAuthn never makes a request.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
from collections.abc import Iterator
from pathlib import Path

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from dreamjob.config import get_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:5173"
RP_ID = "localhost"
PASSWORD = "Str0ng-Passphrase!2026"

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_WEBAUTHN_RP_ID",
    "DREAMJOB_WEBAUTHN_ORIGINS",
)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    """One P-256 passkey, held in memory like a platform authenticator holds it."""

    UP, UV, AT = 0x01, 0x04, 0x40

    def __init__(self, *, user_verified: bool = True, rp_id: str = RP_ID) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.counter = 0
        self.user_verified = user_verified
        self.rp_id = rp_id
        self.user_handle = b""

    def _flags(self, extra: int = 0) -> int:
        return self.UP | (self.UV if self.user_verified else 0) | extra

    def _cose_public_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        )

    def _client_data(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict, *, origin: str = ORIGIN) -> dict:
        self.user_handle = b64url_decode(options["user"]["id"])
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags(self.AT)])
            + struct.pack(">I", self.counter)
            + bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict, *, origin: str = ORIGIN, key=None) -> dict:
        self.counter += 1
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags()])
            + struct.pack(">I", self.counter)
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signer = key or self.key
        signature = signer.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(self.user_handle),
            },
        }


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ.pop("DREAMJOB_WEBAUTHN_RP_ID", None)
    os.environ.pop("DREAMJOB_WEBAUTHN_ORIGINS", None)
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


@pytest.fixture
def client() -> Iterator[TestClient]:
    from dreamjob.api.routers import auth as auth_router

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    with TestClient(app) as c:
        yield c


def _register_account(client: TestClient, email: str = "owner@example.com") -> dict:
    resp = client.post(
        "/api/auth/register",
        json={"email": email, "display_name": "Test Owner", "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _enrol(client: TestClient, authenticator: SoftAuthenticator, name: str = "Laptop"):
    started = client.post("/api/auth/me/passkeys/options").json()
    credential = authenticator.create(started["options"])
    return client.post(
        "/api/auth/me/passkeys",
        json={"ceremony_id": started["ceremony_id"], "credential": credential, "name": name},
    )


def _sign_in(client: TestClient, authenticator: SoftAuthenticator, **kwargs):
    started = client.post("/api/auth/passkey/options", json={}).json()
    credential = authenticator.get(started["options"], **kwargs)
    return client.post(
        "/api/auth/passkey/login",
        json={"ceremony_id": started["ceremony_id"], "credential": credential},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_a_signed_in_seeker_registers_a_passkey(client):
    _register_account(client)
    authenticator = SoftAuthenticator()

    started = client.post("/api/auth/me/passkeys/options").json()
    options = started["options"]
    assert options["rp"]["id"] == RP_ID
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["authenticatorSelection"]["residentKey"] == "required"

    resp = client.post(
        "/api/auth/me/passkeys",
        json={"ceremony_id": started["ceremony_id"],
              "credential": authenticator.create(options), "name": "Laptop"},
    )

    assert resp.status_code == 201, resp.text
    listed = client.get("/api/auth/me/passkeys").json()
    assert [p["name"] for p in listed] == ["Laptop"]
    assert "public_key" not in listed[0]


def test_registration_needs_a_session(client):
    assert client.post("/api/auth/me/passkeys/options").status_code == 401


def test_a_passkey_without_user_verification_is_refused(client):
    """Without UV a passkey is one factor; NFR-202's passkey branch is two."""
    _register_account(client)

    resp = _enrol(client, SoftAuthenticator(user_verified=False))

    assert resp.status_code == 400
    assert client.get("/api/auth/me/passkeys").json() == []


def test_a_response_from_another_origin_is_refused(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    started = client.post("/api/auth/me/passkeys/options").json()

    resp = client.post(
        "/api/auth/me/passkeys",
        json={"ceremony_id": started["ceremony_id"],
              "credential": authenticator.create(started["options"],
                                                 origin="https://evil.example")},
    )

    assert resp.status_code == 400


def test_a_registration_ceremony_is_single_use(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    started = client.post("/api/auth/me/passkeys/options").json()
    body = {"ceremony_id": started["ceremony_id"],
            "credential": authenticator.create(started["options"])}

    assert client.post("/api/auth/me/passkeys", json=body).status_code == 201
    replay = client.post("/api/auth/me/passkeys", json=body)

    assert replay.status_code == 400
    assert "expired or was already used" in replay.json()["detail"]


def test_the_same_passkey_cannot_be_registered_twice(client):
    _register_account(client)
    authenticator = SoftAuthenticator()

    assert _enrol(client, authenticator).status_code == 201
    assert _enrol(client, authenticator).status_code == 400


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


def test_a_registered_passkey_signs_in_without_the_password(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    assert _enrol(client, authenticator).status_code == 201
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401

    resp = _sign_in(client, authenticator)

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "owner@example.com"
    assert client.get("/api/auth/me").status_code == 200
    stored = client.get("/api/auth/me/passkeys").json()[0]
    assert stored["last_used_at"]


def test_the_password_and_mfa_branch_still_works_beside_it(client):
    _register_account(client)
    assert _enrol(client, SoftAuthenticator()).status_code == 201
    client.post("/api/auth/logout")

    resp = client.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD})

    assert resp.status_code == 200


def test_a_signature_from_another_key_is_refused(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    client.post("/api/auth/logout")

    resp = _sign_in(client, authenticator, key=ec.generate_private_key(ec.SECP256R1()))

    assert resp.status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_an_unknown_passkey_is_refused(client):
    _register_account(client)
    client.post("/api/auth/logout")

    assert _sign_in(client, SoftAuthenticator()).status_code == 401


def test_a_sign_in_without_user_verification_is_refused(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    client.post("/api/auth/logout")
    authenticator.user_verified = False

    assert _sign_in(client, authenticator).status_code == 401


def test_an_assertion_cannot_be_replayed(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    client.post("/api/auth/logout")
    started = client.post("/api/auth/passkey/options", json={}).json()
    body = {"ceremony_id": started["ceremony_id"],
            "credential": authenticator.get(started["options"])}

    assert client.post("/api/auth/passkey/login", json=body).status_code == 200
    assert client.post("/api/auth/passkey/login", json=body).status_code == 401


def test_a_cloned_authenticator_is_caught_by_its_counter(client):
    """A counter that goes backwards means two devices hold one key."""
    _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    client.post("/api/auth/logout")
    authenticator.counter = 10
    assert _sign_in(client, authenticator).status_code == 200
    client.post("/api/auth/logout")

    authenticator.counter = 3
    assert _sign_in(client, authenticator).status_code == 401


def test_a_registration_ceremony_cannot_be_spent_on_a_sign_in(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    started = client.post("/api/auth/me/passkeys/options").json()
    client.post("/api/auth/logout")

    resp = client.post(
        "/api/auth/passkey/login",
        json={"ceremony_id": started["ceremony_id"],
              "credential": authenticator.get(started["options"])},
    )

    assert resp.status_code == 401


def test_a_suspended_account_cannot_sign_in_with_a_passkey(client):
    from dreamjob.db.repositories import seekers as repo

    body = _register_account(client)
    authenticator = SoftAuthenticator()
    _enrol(client, authenticator)
    client.post("/api/auth/logout")
    repo.set_disabled(body["id"], True)

    assert _sign_in(client, authenticator).status_code == 401


def test_options_do_not_reveal_which_addresses_exist(client):
    _register_account(client)
    _enrol(client, SoftAuthenticator())
    client.post("/api/auth/logout")

    known = client.post("/api/auth/passkey/options", json={"email": "owner@example.com"}).json()
    unknown = client.post("/api/auth/passkey/options", json={"email": "nobody@example.com"}).json()

    assert len(known["options"]["allowCredentials"]) == 1
    assert unknown["options"]["allowCredentials"] == []


def test_a_removed_passkey_no_longer_signs_in(client):
    _register_account(client)
    authenticator = SoftAuthenticator()
    credential_id = _enrol(client, authenticator).json()["id"]

    assert client.delete(f"/api/auth/me/passkeys/{credential_id}").json() == {"removed": True}
    assert client.delete(f"/api/auth/me/passkeys/{credential_id}").status_code == 404
    client.post("/api/auth/logout")

    assert _sign_in(client, authenticator).status_code == 401


def test_one_seekers_passkey_never_opens_another_account(client):
    _register_account(client, "first@example.com")
    first = SoftAuthenticator()
    _enrol(client, first)
    client.post("/api/auth/logout")
    _register_account(client, "second@example.com")
    _enrol(client, SoftAuthenticator())
    client.post("/api/auth/logout")

    resp = _sign_in(client, first)

    assert resp.json()["email"] == "first@example.com"
