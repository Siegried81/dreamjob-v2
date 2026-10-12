"""Passkeys - WebAuthn registration and sign-in (NFR-202).

NFR-202 asks for "passkeys or strong passwords with MFA".  The password + TOTP
branch is in :mod:`dreamjob.security.auth_service`; this module is the passkey
branch, alongside it rather than instead of it.  A job seeker registers a
passkey while signed in, and can then sign in with it alone: a passkey is
possession of a device-bound key plus the device's own user verification
(PIN or biometric), which is why ``user_verification`` is *required* on every
ceremony - a passkey without it would be one factor, not two.

The cryptography is not written here.  ``webauthn`` (py_webauthn, Duo Labs)
parses the attestation and assertion, checks the challenge, the origin, the
relying-party id hash, the flags and the signature; this module only decides
what to ask for, what to store, and whose account a verified assertion opens.

What is stored
--------------
``job_seeker.webauthn_credentials``, a JSON list - the column the initial
schema reserved for this.  Each entry is the credential id, its public key
(COSE, base64url), the signature counter, the transports and a name.  Nothing
in it is secret: a public key cannot sign.

Challenges
----------
Held in memory for :data:`CHALLENGE_TTL_SECONDS`, keyed by a random ceremony
id handed to the browser, and consumed on first use, so a captured response
cannot be replayed.  Dream Job runs as one local process, so memory is the
whole of its state; a restart simply cancels a ceremony in progress.

Relying party
-------------
``DREAMJOB_WEBAUTHN_RP_ID`` (default ``localhost``) and
``DREAMJOB_WEBAUTHN_ORIGINS`` (default the Vite dev server and the API on
localhost).  WebAuthn refuses an IP address as a relying-party id, so the
application has to be opened as ``http://localhost:...``, not
``http://127.0.0.1:...``, for passkeys to work.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import seekers as repo
from dreamjob.security.audit import record_audit

CHALLENGE_TTL_SECONDS = 300
RP_NAME = "Dream Job"
#: A name is for the job seeker to tell their devices apart; it is never parsed.
MAX_NAME_LENGTH = 80
#: Enough for every device a person plausibly owns; bounds the JSON column.
MAX_PASSKEYS = 20


class PasskeyError(RuntimeError):
    """A ceremony failed; the message is safe to show the job seeker."""


@dataclass
class _Ceremony:
    purpose: str            # "register" | "authenticate"
    challenge: bytes
    seeker_id: str | None
    expires: float


_CEREMONIES: dict[str, _Ceremony] = {}
_LOCK = threading.Lock()


def _remember(purpose: str, challenge: bytes, seeker_id: str | None) -> str:
    ceremony_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _LOCK:
        for key in [k for k, c in _CEREMONIES.items() if c.expires <= now]:
            del _CEREMONIES[key]
        _CEREMONIES[ceremony_id] = _Ceremony(
            purpose, challenge, seeker_id, now + CHALLENGE_TTL_SECONDS
        )
    return ceremony_id


def _take(ceremony_id: str, purpose: str) -> _Ceremony:
    """Consume a ceremony: single use, unexpired, and of the expected purpose."""
    with _LOCK:
        ceremony = _CEREMONIES.pop(ceremony_id, None)
    if ceremony is None or ceremony.purpose != purpose or ceremony.expires <= time.monotonic():
        raise PasskeyError("This passkey request has expired or was already used. Try again.")
    return ceremony


def _rp_id() -> str:
    return (get_settings().webauthn_rp_id or "localhost").strip()


def _origins() -> list[str]:
    raw = get_settings().webauthn_origins or ""
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _stored(row: dict | None) -> list[dict[str, Any]]:
    if not row or not row.get("webauthn_credentials"):
        return []
    try:
        value = json.loads(row["webauthn_credentials"])
    except ValueError:
        return []
    return [c for c in value if isinstance(c, dict) and c.get("id")] if isinstance(value, list) else []


def _transports(entry: dict) -> list[AuthenticatorTransport]:
    out = []
    for value in entry.get("transports") or []:
        try:
            out.append(AuthenticatorTransport(value))
        except ValueError:
            continue
    return out


def _descriptor(entry: dict) -> PublicKeyCredentialDescriptor:
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(entry["id"]), transports=_transports(entry) or None
    )


# ---------------------------------------------------------------------------
# Listing and removing
# ---------------------------------------------------------------------------


def list_passkeys(seeker_id: str) -> list[dict[str, Any]]:
    """What the settings screen shows: never the public key, which nobody reads."""
    return [
        {k: entry.get(k) for k in ("id", "name", "created_at", "last_used_at", "backed_up")}
        for entry in _stored(repo.get_seeker(seeker_id))
    ]


def remove_passkey(seeker_id: str, credential_id: str) -> bool:
    entries = _stored(repo.get_seeker(seeker_id))
    kept = [e for e in entries if e["id"] != credential_id]
    if len(kept) == len(entries):
        return False
    repo.set_webauthn_credentials(seeker_id, kept)
    record_audit(
        "job_seeker.passkey_removed", "job_seeker", seeker_id, seeker_id=seeker_id,
        detail={"credential_id": credential_id},
    )
    return True


# ---------------------------------------------------------------------------
# Registration (signed in)
# ---------------------------------------------------------------------------


def registration_options(seeker_id: str) -> dict[str, Any]:
    """Options for ``navigator.credentials.create``, and the ceremony they belong to."""
    row = repo.get_seeker(seeker_id)
    if row is None:
        raise PasskeyError("No such account")
    existing = _stored(row)
    if len(existing) >= MAX_PASSKEYS:
        raise PasskeyError(f"At most {MAX_PASSKEYS} passkeys can be registered; remove one first.")
    options = generate_registration_options(
        rp_id=_rp_id(),
        rp_name=RP_NAME,
        user_id=seeker_id.encode("utf-8"),
        user_name=row["email"],
        user_display_name=row["display_name"],
        # A discoverable credential is what lets the sign-in screen offer the
        # passkey before an e-mail address is typed.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[_descriptor(e) for e in existing],
    )
    ceremony_id = _remember("register", options.challenge, seeker_id)
    return {"ceremony_id": ceremony_id, "options": _as_dict(options)}


def register(
    seeker_id: str, ceremony_id: str, credential: dict[str, Any], name: str | None = None
) -> dict[str, Any]:
    """Verify the authenticator's answer and store the new passkey."""
    ceremony = _take(ceremony_id, "register")
    if ceremony.seeker_id != seeker_id:
        raise PasskeyError("This passkey request belongs to another session.")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=ceremony.challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_origins(),
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as exc:
        raise PasskeyError(f"The passkey could not be verified: {exc}") from exc

    credential_id = bytes_to_base64url(verified.credential_id)
    entries = _stored(repo.get_seeker(seeker_id))
    if any(e["id"] == credential_id for e in entries) or repo.seeker_by_credential_id(
        credential_id
    ):
        raise PasskeyError("This passkey is already registered.")
    entry = {
        "id": credential_id,
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": int(verified.sign_count),
        "transports": [
            t for t in (credential.get("response") or {}).get("transports") or []
            if isinstance(t, str)
        ],
        "name": (name or "").strip()[:MAX_NAME_LENGTH] or "Passkey",
        "backed_up": bool(verified.credential_backed_up),
        "created_at": utcnow(),
        "last_used_at": None,
    }
    repo.set_webauthn_credentials(seeker_id, [*entries, entry])
    record_audit(
        "job_seeker.passkey_registered", "job_seeker", seeker_id, seeker_id=seeker_id,
        detail={"credential_id": credential_id, "name": entry["name"]},
    )
    return {k: entry[k] for k in ("id", "name", "created_at", "backed_up")}


# ---------------------------------------------------------------------------
# Sign-in (no session yet)
# ---------------------------------------------------------------------------


def authentication_options(email: str | None = None) -> dict[str, Any]:
    """Options for ``navigator.credentials.get``.

    Without an e-mail the browser offers whichever discoverable passkey it
    holds for this site.  With one, the account's own passkeys are listed - and
    an unknown address gets the same, empty, answer as an account with none,
    so the endpoint does not reveal which addresses are registered.
    """
    allow: list[PublicKeyCredentialDescriptor] = []
    if email:
        allow = [_descriptor(e) for e in _stored(repo.get_seeker_by_email(email))]
    options = generate_authentication_options(
        rp_id=_rp_id(),
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    ceremony_id = _remember("authenticate", options.challenge, None)
    return {"ceremony_id": ceremony_id, "options": _as_dict(options)}


def authenticate(ceremony_id: str, credential: dict[str, Any]) -> dict:
    """Verify an assertion and return the job seeker row it opens.

    Raises :class:`PasskeyError` for anything that does not verify, with one
    message for an unknown credential and a bad signature alike.
    """
    ceremony = _take(ceremony_id, "authenticate")
    credential_id = str(credential.get("id") or credential.get("rawId") or "")
    row = repo.seeker_by_credential_id(credential_id) if credential_id else None
    entry = next((e for e in _stored(row) if e["id"] == credential_id), None)
    if row is None or entry is None:
        raise PasskeyError("This passkey is not registered with Dream Job.")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=ceremony.challenge,
            expected_rp_id=_rp_id(),
            expected_origin=_origins(),
            credential_public_key=base64url_to_bytes(entry["public_key"]),
            credential_current_sign_count=int(entry.get("sign_count") or 0),
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        record_audit(
            "session.passkey_failed", "job_seeker", row["id"], seeker_id=row["id"],
            detail={"credential_id": credential_id},
        )
        raise PasskeyError("The passkey could not be verified.") from exc
    if row.get("disabled"):
        raise PasskeyError("This account has been suspended")

    entries = _stored(row)
    for stored in entries:
        if stored["id"] == credential_id:
            stored["sign_count"] = int(verified.new_sign_count)
            stored["last_used_at"] = utcnow()
            stored["backed_up"] = bool(verified.credential_backed_up)
    repo.set_webauthn_credentials(row["id"], entries)
    return row


def _as_dict(options: Any) -> dict[str, Any]:
    """The options as the browser's JSON form (base64url for every byte string)."""
    return json.loads(options_to_json(options))
