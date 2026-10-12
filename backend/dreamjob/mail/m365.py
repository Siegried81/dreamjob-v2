"""Microsoft 365 / Outlook.com mailbox over Microsoft Graph, read-only (FR-326, NFR-204).

What it is for
--------------
Reply and bounce detection for a job seeker whose mailbox is Microsoft's -
an Exchange Online work or school account, or an Outlook.com / Hotmail
personal one.  Their replies arrive in that inbox; this backend lets the
poller read them, exactly as it reads a Gmail inbox.

It is **read-only by construction**.  The only Graph permission requested is
``Mail.Read`` (with ``User.Read`` to learn the address and ``offline_access``
for a refresh token), so the token itself cannot send, move, delete or flag a
message - not merely "this code does not".  Graph does not mark a message read
when it is fetched with ``GET``, and nothing here issues anything but ``GET``
against the mailbox.  ``can_send`` is ``False``: the dispatcher never chooses
this backend to send from, and :meth:`M365GraphBackend.send` refuses if asked.

Flow
----
Authorisation code with PKCE and a loopback redirect, the flow Microsoft
prescribes for an installed application ("Mobile and desktop applications"
platform; ``M365_REDIRECT_URI``, ``http://localhost:8000/api/mail/m365/callback``
by default).  Such a client is public: no secret is sent unless
``M365_CLIENT_SECRET`` is set, which is only needed when the app was
registered under the "Web" platform instead.  ``M365_TENANT`` is ``common``
(work, school and personal accounts) unless the registration is narrowed.
The single-use ``state`` row binds the redirect to the request that started it.

Revocation
----------
Microsoft offers no endpoint to revoke one refresh token from a public client,
so disconnecting wipes the stored credential here (it cannot be used from this
installation again) and returns the page where the job seeker removes the
app's consent: https://myapps.microsoft.com for a work account,
https://account.live.com/consent/Manage for a personal one.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from dreamjob.config import get_settings
from dreamjob.db.repositories import dispatch as repo
from dreamjob.mail.base import (
    BackendCapabilities,
    MailBackend,
    MailBackendError,
    MailBackendNotConfigured,
    MailBackendUnavailable,
    OutgoingMessage,
    SendResult,
    decrypt_credentials,
    encrypt_credentials,
    register_backend,
)

log = logging.getLogger(__name__)

BACKEND_KEY = "m365_graph"
LOGIN_ROOT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

#: NFR-204: read the mail, know whose mailbox it is, keep access between runs.
#: Nothing that can write: no Mail.ReadWrite, no Mail.Send.
SCOPES: tuple[str, ...] = (
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/User.Read",
    "offline_access",
)

STATE_TTL_SECONDS = 600
EXPIRY_SKEW_SECONDS = 120
#: Where the job seeker finishes a disconnection, by account type.
CONSENT_PAGES = {
    "work_or_school": "https://myapps.microsoft.com",
    "personal": "https://account.live.com/consent/Manage",
}


class M365AuthError(MailBackendError):
    """The OAuth handshake or a token refresh failed."""


def _encrypt(credentials: dict, job_seeker_id: str) -> bytes:
    return encrypt_credentials(credentials, job_seeker_id, what="Microsoft 365 mailbox")


def _decrypt(blob: bytes | None, job_seeker_id: str) -> dict:
    return decrypt_credentials(
        blob,
        job_seeker_id,
        what="Microsoft 365 mailbox",
        reconnect_hint="Open Settings > Mail and connect it again.",
    )


def _endpoint(name: str) -> str:
    tenant = (get_settings().m365_tenant or "common").strip() or "common"
    return f"{LOGIN_ROOT.format(tenant=tenant)}/{name}"


def _not_set_up() -> MailBackendNotConfigured:
    s = get_settings()
    return MailBackendNotConfigured(
        "Microsoft 365 is not set up on this installation: M365_CLIENT_ID is empty. Register "
        "an application in the Microsoft Entra admin centre (platform 'Mobile and desktop "
        f"applications', redirect URI {s.m365_redirect_uri}, delegated permission Mail.Read) "
        "and put its Application (client) ID in .env. See docs/M365_Mail_Setup.md."
    )


# ---------------------------------------------------------------------------
# Authorisation code flow with PKCE
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def authorization_url(job_seeker_id: str, *, login_hint: str | None = None) -> dict:
    """Start the handshake.  Returns the URL to open and the state it is bound to."""
    s = get_settings()
    if not s.m365_client_id:
        raise _not_set_up()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    scope = " ".join(SCOPES)
    repo.create_oauth_state(
        state,
        job_seeker_id=job_seeker_id,
        backend=BACKEND_KEY,
        code_verifier=verifier,
        redirect_uri=s.m365_redirect_uri,
        scopes=scope,
        expires_at=(datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS)).isoformat(
            timespec="seconds"
        ),
    )
    params = {
        "client_id": s.m365_client_id,
        "redirect_uri": s.m365_redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Ask every time, so a re-connection shows exactly what is granted.
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = str(httpx.URL(_endpoint("authorize"), params=params))
    return {"authorization_url": url, "state": state, "scopes": list(SCOPES)}


def _token_request(payload: dict) -> dict:
    s = get_settings()
    payload = {"client_id": s.m365_client_id, **payload}
    # A public client must not send a secret; one registered as a Web app must.
    if s.m365_client_secret:
        payload["client_secret"] = s.m365_client_secret
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(_endpoint("token"), data=payload)
    except httpx.HTTPError as exc:
        raise MailBackendUnavailable(f"Could not reach Microsoft's token endpoint: {exc}") from exc
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('error_description', '')}".strip(": ")
        except ValueError:
            detail = resp.text[:300]
        raise M365AuthError(f"Microsoft rejected the token request ({resp.status_code}): {detail}")
    return resp.json()


def _expiry_iso(expires_in: Any) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(UTC) + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")


def complete_authorization(state: str, code: str) -> dict:
    """Finish the loopback redirect: exchange the code and store the mailbox."""
    row = repo.take_oauth_state(state)
    if row is None or row.get("backend") != BACKEND_KEY:
        raise M365AuthError(
            "This authorisation link has expired or was already used. Start the connection again."
        )
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": row["code_verifier"],
            "redirect_uri": row["redirect_uri"],
            "scope": row["scopes"] or " ".join(SCOPES),
        }
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise M365AuthError(
            "Microsoft returned no refresh token; the offline_access permission is missing from "
            "the app registration. Add it and connect the mailbox again."
        )
    access_token = str(tokens.get("access_token") or "")
    granted = str(tokens.get("scope") or row["scopes"] or " ".join(SCOPES))
    _refuse_write_scopes(granted)
    address = _profile_address(access_token)
    credentials = {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "access_token_expires_at": _expiry_iso(tokens.get("expires_in")),
        "scope": granted,
    }
    account_id = repo.upsert_account(
        row["job_seeker_id"],
        backend=BACKEND_KEY,
        address=address,
        credentials_enc=_encrypt(credentials, row["job_seeker_id"]),
        scopes=granted,
        token_expires_at=credentials["access_token_expires_at"],
    )
    log.info("Connected Microsoft 365 mailbox for seeker %s", row["job_seeker_id"])
    return {
        "id": account_id,
        "job_seeker_id": row["job_seeker_id"],
        "backend": BACKEND_KEY,
        "address": address,
        "scopes": granted,
    }


#: Graph permissions that could change a mailbox.  A token carrying one is
#: refused at connection, so "read-only" stays true even if the registration
#: was later widened by somebody else.
WRITE_SCOPES = ("mail.readwrite", "mail.send", "mailboxsettings.readwrite")


def _refuse_write_scopes(granted: str) -> None:
    lowered = {s.rsplit("/", 1)[-1].lower() for s in granted.split()}
    wider = sorted(lowered & set(WRITE_SCOPES))
    if wider:
        raise M365AuthError(
            "The token Microsoft issued can change the mailbox (" + ", ".join(wider) + "); "
            "Dream Job reads mail only. Remove those permissions from the app registration "
            "and connect again."
        )


def _profile_address(access_token: str) -> str:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{GRAPH_ROOT}/me",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "mail,userPrincipalName"},
            )
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise M365AuthError(f"Could not read the Microsoft profile after authorising: {exc}") from exc
    address = str(body.get("mail") or body.get("userPrincipalName") or "").lower()
    if "@" not in address:
        raise M365AuthError("Microsoft returned no mail address for this account.")
    return address


def disconnect(account: dict) -> dict:
    """Wipe the stored credential; say where the consent itself is removed."""
    repo.clear_credentials(account["id"])
    return {
        "revoked_locally": True,
        "revoked_at_provider": False,
        "finish_at": CONSENT_PAGES,
        "note": (
            "Microsoft has no endpoint to revoke a single refresh token from an installed "
            "app. The credential is gone from Dream Job; remove the app's consent on the "
            "page for your account type to finish."
        ),
    }


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


@register_backend
class M365GraphBackend(MailBackend):
    """Reads a Microsoft 365 / Outlook.com inbox through Graph; never sends."""

    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        key=BACKEND_KEY,
        display_name="Microsoft 365 / Outlook.com (read-only, Graph)",
        can_send=False,
        replies_to_seeker_inbox=True,
        bounce_detection="graph_api",
        reply_detection="poll",
        requires_oauth=True,
        supports_attachments=False,
    )

    def __init__(self, account: dict | None = None, *, job_seeker_id: str | None = None):
        super().__init__(account, job_seeker_id=job_seeker_id)
        self._credentials: dict | None = None

    @property
    def credentials(self) -> dict:
        if self._credentials is None:
            if not self.account:
                raise MailBackendNotConfigured(
                    "No Microsoft 365 mailbox is connected for this job seeker. "
                    "Open Settings > Mail and connect one."
                )
            self._credentials = _decrypt(self.account.get("credentials_enc"), str(self.job_seeker_id))
        return self._credentials

    def access_token(self) -> str:
        """A live access token, refreshed when it is within the skew of expiry."""
        credentials = self.credentials
        expiry = credentials.get("access_token_expires_at")
        if credentials.get("access_token") and expiry:
            deadline = datetime.fromisoformat(expiry) - timedelta(seconds=EXPIRY_SKEW_SECONDS)
            if datetime.now(UTC) < deadline:
                return str(credentials["access_token"])
        if not get_settings().m365_client_id:
            raise _not_set_up()
        tokens = _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
                "scope": " ".join(SCOPES),
            }
        )
        credentials["access_token"] = str(tokens.get("access_token") or "")
        credentials["access_token_expires_at"] = _expiry_iso(tokens.get("expires_in"))
        # Microsoft rotates refresh tokens; keeping the old one would disconnect
        # the mailbox once it expires.
        if tokens.get("refresh_token"):
            credentials["refresh_token"] = tokens["refresh_token"]
        if self.account.get("id"):
            repo.set_account_credentials(
                self.account["id"],
                _encrypt(credentials, str(self.job_seeker_id)),
                token_expires_at=credentials["access_token_expires_at"],
            )
        return str(credentials["access_token"])

    def _get(self, url: str, *, params: dict | None = None) -> httpx.Response:
        """One ``GET`` against Graph - the only verb this backend uses on a mailbox."""
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token()}"},
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise MailBackendUnavailable(f"Microsoft Graph unreachable: {exc}") from exc
        if resp.status_code in (401, 403):
            raise MailBackendNotConfigured(
                f"Microsoft Graph refused the mailbox read ({resp.status_code}); reconnect it."
            )
        if resp.status_code >= 400:
            raise MailBackendUnavailable(f"Microsoft Graph returned {resp.status_code}")
        return resp

    # -- reading, for FR-326 ----------------------------------------------
    def list_message_ids(self, since: str | None, *, max_results: int = 100) -> list[str]:
        """Inbox messages received at or after ``since`` (ISO-8601), oldest first."""
        params: dict[str, Any] = {
            "$select": "id,receivedDateTime",
            "$orderby": "receivedDateTime asc",
            "$top": str(min(50, max_results)),
        }
        if since:
            stamp = datetime.fromisoformat(since).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            params["$filter"] = f"receivedDateTime ge {stamp}"
        url: str | None = f"{GRAPH_ROOT}/me/mailFolders/inbox/messages"
        out: list[str] = []
        while url and len(out) < max_results:
            body = self._get(url, params=params).json()
            out.extend(str(m["id"]) for m in body.get("value") or [] if m.get("id"))
            # The next link carries every query parameter already.
            url, params = body.get("@odata.nextLink"), None
        return out[:max_results]

    def fetch_raw(self, message_id: str) -> bytes:
        """The full MIME source (``$value``), so bounce reports parse structurally."""
        return self._get(f"{GRAPH_ROOT}/me/messages/{message_id}/$value").content

    # -- MailBackend ------------------------------------------------------
    def sender_address(self) -> str:
        raise MailBackendError("The Microsoft 365 connection is read-only; it never sends.")

    def send(self, message: OutgoingMessage) -> SendResult:
        raise MailBackendError(
            "The Microsoft 365 connection is read-only: it detects replies and bounces but "
            "cannot send. Connect Gmail or an IMAP mailbox to send from."
        )

    def status(self) -> dict:
        s = get_settings()
        base = {"backend": self.key, "capabilities": self.capabilities.__dict__}
        if not s.m365_client_id:
            return {**base, "configured": False,
                    "reason": "M365_CLIENT_ID is not set in .env (see docs/M365_Mail_Setup.md)"}
        if not self.account or not self.account.get("credentials_enc"):
            return {**base, "configured": False,
                    "reason": "No Microsoft 365 mailbox connected yet - run the authorisation flow",
                    "authorize_path": "/api/mail/m365/authorize"}
        return {
            **base,
            "configured": True,
            "read_only": True,
            "address": self.account.get("address"),
            "scopes": sorted(str(self.account.get("scopes") or "").split()),
            "token_expires_at": self.account.get("token_expires_at"),
        }
