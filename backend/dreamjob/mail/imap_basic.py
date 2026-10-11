"""Any IMAP + SMTP mailbox, authorised with an application password (FR-325, FR-326, NFR-204).

Why this exists beside ``gmail_oauth``
-------------------------------------
``gmail_oauth`` is the better credential - revocable at the provider, scoped,
no password at rest - but it is Google-only.  Yahoo, iCloud and most self-hosted
servers run no OAuth programme a third-party mail client can join, and the
credential they issue instead is an *application password*: a provider-generated
secret, distinct from the account password, revocable from the provider's own
security page, and useless for signing in to the web account.

That is the credential this backend stores, and it buys the same section 2.4
property ``gmail_oauth`` has: the message leaves the job seeker's own mailbox,
so replies and delivery-status notifications arrive in their own inbox where
FR-326 can read them.

How this differs from :mod:`dreamjob.mail.job_alerts`
-----------------------------------------------------
Both log in to a mailbox over IMAP with an application password, and the two
must not be confused:

* ``job_alerts`` (settings ``DREAMJOB_ALERTS_IMAP_*``, one mailbox configured
  by the operator in ``.env``) is a **read-only scan of known job-alert
  senders** - LinkedIn, ictjob, Glassdoor, Jobat - whose postings become
  *vacancies*.  It never sends and never looks at any other message.
* ``imap_basic`` (this module, one row per job seeker in ``mail_account``,
  connected through ``/api/mail/imap/connect``) is **the mailbox Dream Job
  sends applications from and reads replies to**.  What it reads is matched
  against dispatches and recorded as ``incoming_reply`` rows; it never creates
  a vacancy or an opportunity.

The same Yahoo mailbox may be connected to both; each keeps its own cursor and
neither changes what the other does.

What is stored, and where
-------------------------
Everything needed to reconnect goes in one encrypted blob in
``mail_account.credentials_enc`` - the password together with the host, port
and folder.  The hosts are not secret, but keeping them in the same envelope
means one mailbox is one row with no schema change, and a job seeker with two
mailboxes on different providers is just two rows.  The envelope itself is
``base.encrypt_credentials``, so the password is encrypted per job seeker
(NFR-204) exactly as the Gmail refresh token is.

The credential is verified against the provider *before* it is stored
(:func:`connect`), because a password that only fails at the next poll is a
silent mailbox: the poller swallows per-account errors so one bad mailbox
cannot stop the others, which is right for the job and wrong for setup.

Connecting to a host the job seeker names
-----------------------------------------
``/api/mail/imap/connect`` accepts custom hosts, so the server opens sockets to
places a user chose.  :func:`check_endpoint` therefore refuses any host that
resolves - on *any* of its addresses - to a loopback, private, link-local
(cloud metadata included), multicast, reserved or otherwise non-public address,
and any port other than the mail ones (IMAP 993, SMTP 465 or 587).  It runs at
connect, before every poll (:meth:`ImapBasicBackend.imap_endpoint`) and before
every send, so a name whose DNS later changes is caught on its next use.  Every
socket uses :func:`dreamjob.mail.tls.verified_context` and a timeout, and errors shown to
the client are fixed sentences: a provider's own text, which an attacker could
use as a port-scanning oracle, is logged server-side by exception type only.

Note on reading (FR-326)
------------------------
The poller reads this mailbox through :func:`dreamjob.mail.inbox.poll_imap`:
that function asks the backend for its session (:meth:`open_imap`, connected to
the address the host check approved) and its password, so the only other
difference from Gmail-over-IMAP is that :meth:`imap_password` answers with a
password instead of ``None``.  Reading is also all it does - a message
that matches no dispatch Dream Job sent is recorded and classified, never
turned into an opportunity, and nothing here ever deletes or moves mail.  The
IMAP session is opened ``readonly``.
"""

from __future__ import annotations

import email.errors
import imaplib
import ipaddress
import logging
import re
import smtplib
import socket
import ssl
from typing import ClassVar

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
    imap_mailbox,
    register_backend,
)
from dreamjob.mail.tls import verified_context

log = logging.getLogger(__name__)

BACKEND_KEY = "imap_basic"

#: How long to wait on a provider socket.  Deliberately short for the login
#: probe in :func:`connect` (someone is watching a form) and longer for a send,
#: which carries a CV attachment.
PROBE_TIMEOUT = 20.0
SEND_TIMEOUT = 120.0

#: The only ports a mailbox may name.  IMAP is implicit TLS only; SMTP is
#: implicit TLS (465) or STARTTLS (587), and 587 is upgraded on the same
#: verifying context, with a refusal to upgrade being an error rather than a
#: plaintext fallback.  Anything else would let the connect form probe
#: arbitrary services.
IMAP_PORTS: frozenset[int] = frozenset({993})
SMTP_PORTS: frozenset[int] = frozenset({465, 587})
SMTP_STARTTLS_PORT = 587

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465

#: Characters that never belong in an address, a host or a folder name.  CR/LF
#: and NUL would split an IMAP or SMTP command line; quotes would break out of
#: IMAP's quoted-string argument.
_FORBIDDEN_CHARS = frozenset('\r\n\0"\'\\')
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")


@register_backend
class ImapBasicBackend(MailBackend):
    """Send over SMTP, read over IMAP, authorised by an application password."""

    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        key=BACKEND_KEY,
        display_name="IMAP / SMTP (app password)",
        # Same section 2.4 standing as Gmail: it is the job seeker's own mailbox.
        replies_to_seeker_inbox=True,
        bounce_detection="imap",
        reply_detection="poll",
        requires_oauth=False,
        # The lowest common ceiling among the providers this targets; Yahoo and
        # iCloud both stop at 25 MB, so a larger message would be accepted here
        # and bounced there, which FR-326 would then have to explain.
        max_message_bytes=25 * 1024 * 1024,
    )

    def __init__(self, account: dict | None = None, *, job_seeker_id: str | None = None):
        super().__init__(account, job_seeker_id=job_seeker_id)
        self._credentials: dict | None = None

    # -- credentials ------------------------------------------------------
    @property
    def credentials(self) -> dict:
        if self._credentials is None:
            if not self.account:
                raise MailBackendNotConfigured(
                    "No IMAP mailbox is connected for this job seeker. "
                    "Open Settings > Mail and connect one."
                )
            self._credentials = decrypt_credentials(
                self.account.get("credentials_enc"),
                str(self.job_seeker_id),
                what="IMAP mailbox",
                reconnect_hint="Open Settings > Mail and reconnect it with a fresh app password.",
            )
        return self._credentials

    # -- the IMAP transport's view of this mailbox (see mail.inbox) --------
    def imap_endpoint(self) -> tuple[str, int]:
        """Host and port for the poller, re-checked on every call.

        The check runs here rather than only at connect because the poller
        calls this before each pass: a host whose DNS has since moved to a
        private address is refused on the next poll, not trusted forever.
        """
        c = self.credentials
        host, port = str(c["imap_host"]), int(c["imap_port"])
        check_endpoint(host, port, kind="imap")
        return host, port

    def imap_password(self) -> str | None:
        return str(self.credentials["password"])

    def imap_folder(self) -> str:
        return str(self.credentials.get("folder") or "INBOX")

    def open_imap(self, timeout: float) -> imaplib.IMAP4_SSL:
        """The poller's session, opened to the address the host check approved.

        The poller asks for this rather than building ``IMAP4_SSL`` from
        :meth:`imap_endpoint` itself, because that would resolve the name a
        second time after the check (see :func:`check_endpoint`).
        """
        host, port = self.imap_endpoint()
        return _imap_client(host, port, timeout=timeout)

    # -- MailBackend ------------------------------------------------------
    def sender_address(self) -> str:
        address = self.account.get("address")
        if not address:
            raise MailBackendNotConfigured("The connected IMAP mailbox has no address recorded.")
        return str(address)

    def status(self) -> dict:
        if not self.account or not self.account.get("credentials_enc"):
            return {
                "backend": self.key,
                "configured": False,
                "reason": (
                    "No IMAP mailbox connected yet - connect one with an app password "
                    "from your provider"
                ),
                "connect_path": "/api/mail/imap/connect",
                "providers": sorted(PROVIDERS),
                "capabilities": self.capabilities.__dict__,
            }
        # Read the envelope so a mailbox whose blob cannot be opened (master key
        # changed) reports that here rather than at the next send.
        try:
            c = self.credentials
        except MailBackendError as exc:
            return {
                "backend": self.key,
                "configured": False,
                "reason": str(exc),
                "capabilities": self.capabilities.__dict__,
            }
        return {
            "backend": self.key,
            "configured": True,
            "address": self.account.get("address"),
            "imap_host": c.get("imap_host"),
            "smtp_host": c.get("smtp_host"),
            "folder": c.get("folder", "INBOX"),
            "provider": c.get("provider"),
            "capabilities": self.capabilities.__dict__,
        }

    def send(self, message: OutgoingMessage) -> SendResult:
        """Hand the composed MIME to the provider's SMTP, as the job seeker.

        The composer's ``Message-ID`` is the one that goes on the wire and the
        one returned, which is simpler than the Gmail path: SMTP does not
        rewrite it, so the id already in the send log is the id that will come
        back in a reply's ``References`` (FR-326).

        Composing happens inside the error handling, so a malformed header is a
        ``MailBackendError`` the dispatcher records rather than an HTTP 500.
        """
        from dreamjob.mail.composer import to_mime  # noqa: PLC0415 - avoids an import cycle

        self.check_message(message)
        address = self.sender_address()
        if not message.from_email:
            message.from_email = address

        c = self.credentials
        host, port = str(c["smtp_host"]), int(c["smtp_port"])
        check_endpoint(host, port, kind="smtp")
        try:
            mime = to_mime(message)
            raw = mime.as_bytes()
        except (ValueError, TypeError, UnicodeError, email.errors.MessageError) as exc:
            log.warning("Composing a message for %s failed: %s", host, type(exc).__name__)
            raise MailBackendError(
                "The message could not be composed: one of its headers is malformed."
            ) from exc
        try:
            with _smtp_client(host, port, timeout=SEND_TIMEOUT) as client:
                client.login(address, str(c["password"]))
                client.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            log.warning("SMTP login to %s refused: %s", host, type(exc).__name__)
            raise MailBackendNotConfigured(
                f"{host} refused the app password for {address}. Generate a new "
                "application password with your provider and reconnect the mailbox."
            ) from exc
        except ssl.SSLError as exc:
            log.warning("SMTP TLS with %s failed: %s", host, type(exc).__name__)
            raise MailBackendUnavailable(_tls_failed(host)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("SMTP send through %s failed: %s", host, type(exc).__name__)
            raise MailBackendUnavailable(
                f"Sending through {host} failed: the server was unreachable, timed out "
                "or refused the message."
            ) from exc

        return SendResult(
            message_id=message.message_id,
            thread_id=None,
            status="sent",
            backend=self.key,
            detail={
                "smtp_host": host,
                "composed_message_id": message.message_id,
                "bytes": len(raw),
            },
        )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

#: Hosts and ports per provider, so connecting is "pick Yahoo and paste the app
#: password" rather than four fields nobody remembers.  IMAP is implicit TLS
#: (993) everywhere; SMTP is implicit TLS (465) where the provider offers it and
#: STARTTLS (587) where that is the only port it documents (iCloud, Outlook).
PROVIDERS: dict[str, dict[str, object]] = {
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        # Yahoo issues these at
        # https://login.yahoo.com/account/security -> "Generate app password".
        "app_password_url": "https://login.yahoo.com/account/security",
    },
    "icloud": {
        "label": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "app_password_url": "https://account.apple.com/account/manage",
    },
    "outlook": {
        "label": "Outlook / Hotmail",
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "app_password_url": "https://account.microsoft.com/security",
    },
    "gmail": {
        # Offered for completeness, but gmail_oauth is the better credential and
        # the status screen says so: an app password here is a password at rest.
        "label": "Gmail (app password - prefer the OAuth backend)",
        "imap_host": "imap.gmail.com",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "app_password_url": "https://myaccount.google.com/apppasswords",
    },
}


def provider_choices() -> list[dict]:
    """What the connect screen offers, including where to get the password."""
    return [{"key": key, **spec} for key, spec in PROVIDERS.items()]


def _clean(value: str, what: str) -> str:
    """Refuse characters that could split or escape a protocol line, then strip.

    Checked before stripping, so a trailing CR/LF is refused rather than
    quietly trimmed: input carrying one was not typed into a form.
    """
    if any(ch in _FORBIDDEN_CHARS for ch in value):
        raise MailBackendError(f"The {what} contains a character that is not allowed.")
    return value.strip()


def resolve_provider(
    provider: str | None,
    *,
    imap_host: str | None = None,
    smtp_host: str | None = None,
    imap_port: int | None = None,
    smtp_port: int | None = None,
) -> dict:
    """Turn a provider key, or explicit hosts, into one settings dict.

    Explicit hosts win over the preset, so a self-hosted server needs no entry
    in :data:`PROVIDERS` - which is the point of keeping the presets a
    convenience rather than an allowlist.  What *is* an allowlist is the ports:
    see :data:`IMAP_PORTS` and :data:`SMTP_PORTS`.
    """
    preset: dict[str, object] = {}
    key = (provider or "").strip().lower()
    if key:
        if key not in PROVIDERS:
            raise MailBackendError(
                f"Unknown mail provider; known: {', '.join(sorted(PROVIDERS))}. "
                "Leave it empty and give imap_host / smtp_host directly for anything else."
            )
        preset = PROVIDERS[key]

    resolved_imap = _clean(str(imap_host or preset.get("imap_host") or ""), "IMAP host")
    resolved_smtp = _clean(str(smtp_host or preset.get("smtp_host") or ""), "SMTP host")
    if not resolved_imap or not resolved_smtp:
        raise MailBackendError(
            "An IMAP host and an SMTP host are needed. Either name a provider "
            f"({', '.join(sorted(PROVIDERS))}) or give imap_host and smtp_host."
        )
    for host in (resolved_imap, resolved_smtp):
        if not _HOST_RE.match(host):
            raise MailBackendError("A mail host must be a host name or an IP address.")
    resolved_imap_port = int(imap_port or DEFAULT_IMAP_PORT)
    resolved_smtp_port = int(smtp_port or preset.get("smtp_port") or DEFAULT_SMTP_PORT)
    _check_port(resolved_imap_port, "imap")
    _check_port(resolved_smtp_port, "smtp")
    return {
        "provider": key or "custom",
        "imap_host": resolved_imap,
        "imap_port": resolved_imap_port,
        "smtp_host": resolved_smtp,
        "smtp_port": resolved_smtp_port,
    }


# ---------------------------------------------------------------------------
# Which hosts the server may connect to
# ---------------------------------------------------------------------------


def _check_port(port: int, kind: str) -> None:
    allowed = IMAP_PORTS if kind == "imap" else SMTP_PORTS
    if port not in allowed:
        raise MailBackendError(
            f"Port {port} is not accepted for {kind.upper()}; use "
            f"{' or '.join(str(p) for p in sorted(allowed))}."
        )


def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to.  A seam for the tests (no real DNS)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    """True only for a globally routable unicast address.

    IPv4-mapped IPv6 (``::ffff:10.0.0.1``) is judged as the IPv4 address it
    carries, so the mapping cannot be used to smuggle a private target past the
    check.  ``is_global`` already excludes loopback, RFC 1918, link-local (the
    169.254.169.254 metadata address among them), carrier-grade NAT, documentation
    and reserved ranges; the explicit tests are kept so the intent is legible.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


def check_endpoint(host: str, port: int, *, kind: str) -> str:
    """Refuse a host or port the server must not connect to on a user's behalf.

    Every resolved address is checked, not the first one: a name with one
    public and one private record would otherwise pass the check and still let
    the socket library pick the private one.

    Returns the address that was checked, which is the one the clients below
    connect to.  Handing the name back to :mod:`imaplib` or :mod:`smtplib`
    instead would make them resolve it a second time, and a DNS record that
    answers "public" to the check and "private" a moment later (DNS rebinding)
    would get the connection the check refused.
    """
    _check_port(int(port), kind)
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal.split("%", 1)[0])
        addresses = [literal]
    except ValueError:
        try:
            addresses = _resolve(host, int(port))
        except (OSError, UnicodeError) as exc:
            log.warning("Resolving %s failed: %s", host, type(exc).__name__)
            raise MailBackendUnavailable(f"The mail host {host} could not be resolved.") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise MailBackendError(
            f"The mail host {host} resolves to an address Dream Job will not connect to "
            "(loopback, private, link-local or otherwise not public)."
        )
    return addresses[0]


# ---------------------------------------------------------------------------
# Connecting a mailbox
# ---------------------------------------------------------------------------


def _tls_failed(host: str) -> str:
    return (
        f"The TLS handshake with {host} failed: its certificate is not valid for that "
        "name, or it does not offer TLS 1.2 or later."
    )


class _PinnedIMAPMixin:
    """Open imaplib's socket to an address already checked.

    The certificate is still verified against the host *name* (``self.host``),
    so pinning the address changes where the packets go, not what is trusted.
    ``_create_socket`` is imaplib's own seam for this, unchanged in its
    contract since Python 3.9; the tests drive the real class and assert the
    address the socket was opened to, so a Python that renamed it would fail
    them rather than quietly reconnect by name.
    """

    def __init__(self, host: str, port: int, *, address: str, **kwargs):
        self._pinned_address = address  # set before super(): __init__ connects
        super().__init__(host, port, **kwargs)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._pinned_address, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedSMTPMixin:
    """Route smtplib's socket to the checked address; TLS still names the host.

    ``connect`` keeps the name in ``self._host``, which both ``SMTP_SSL`` and
    ``starttls`` pass as ``server_hostname``, so only the TCP target moves.
    """

    def __init__(self, host: str, port: int, *, address: str, **kwargs):
        self._pinned_address = address  # set before super(): __init__ connects
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        return super()._get_socket(self._pinned_address, port, timeout)


def _pinned(mixin: type, base: type) -> type:
    """``base`` with ``mixin`` in front, built when a connection is opened.

    Not at import: this module is loaded lazily by the backend registry, and a
    class statement binds whatever ``smtplib.SMTP`` is at that moment - a test
    tripwire standing in for it made the import itself fail, taking every mail
    backend down with it.  Building per connection costs nothing next to the
    TLS handshake.
    """
    return type(f"_Pinned{base.__name__}", (mixin, base), {})


def _open_smtp(
    host: str, port: int, *, address: str, timeout: float, context: ssl.SSLContext | None = None
) -> smtplib.SMTP:
    """Plain SMTP (``context`` None, for STARTTLS) or implicit TLS, pinned to ``address``."""
    if context is None:
        return _pinned(_PinnedSMTPMixin, smtplib.SMTP)(
            host, port, address=address, timeout=timeout
        )
    return _pinned(_PinnedSMTPMixin, smtplib.SMTP_SSL)(
        host, port, address=address, context=context, timeout=timeout
    )


def _imap_client(host: str, port: int, *, timeout: float) -> imaplib.IMAP4_SSL:
    """A verified IMAP session to the address :func:`check_endpoint` just approved.

    Indirected so tests can substitute a fake server without a socket.
    """
    address = check_endpoint(host, port, kind="imap")
    return _pinned(_PinnedIMAPMixin, imaplib.IMAP4_SSL)(
        host, port, address=address, ssl_context=verified_context(), timeout=timeout
    )


def _smtp_client(host: str, port: int, *, timeout: float) -> smtplib.SMTP:
    """An SMTP session already under verified TLS, by implicit TLS or STARTTLS.

    Opened to the address :func:`check_endpoint` just approved.  On 587
    ``starttls`` raises when the server does not offer the upgrade, so a
    session never continues in plaintext with the password still to come.
    """
    address = check_endpoint(host, port, kind="smtp")
    context = verified_context()
    if port == SMTP_STARTTLS_PORT:
        client = _open_smtp(host, port, address=address, timeout=timeout)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        except BaseException:
            client.close()
            raise
        return client
    return _open_smtp(host, port, address=address, timeout=timeout, context=context)


def verify_credentials(
    address: str, password: str, settings: dict, *, folder: str = "INBOX"
) -> dict:
    """Log in to both halves before anything is stored.

    IMAP and SMTP are checked separately because providers fail them
    separately: Yahoo, for instance, accepts an app password on IMAP while
    refusing an account that has not enabled SMTP access, and reporting "the
    password is wrong" for that would send someone to regenerate a password
    that was fine.  The messages are fixed sentences; what the server actually
    said is logged by exception type only.
    """
    checked: dict[str, object] = {}
    imap_host, imap_port = str(settings["imap_host"]), int(settings["imap_port"])
    smtp_host, smtp_port = str(settings["smtp_host"]), int(settings["smtp_port"])

    try:
        client = _imap_client(imap_host, imap_port, timeout=PROBE_TIMEOUT)
    except ssl.SSLError as exc:
        log.warning("IMAP TLS with %s failed: %s", imap_host, type(exc).__name__)
        raise MailBackendUnavailable(_tls_failed(imap_host)) from exc
    except (OSError, imaplib.IMAP4.error) as exc:
        log.warning("IMAP connect to %s failed: %s", imap_host, type(exc).__name__)
        raise MailBackendUnavailable(
            f"Could not reach the IMAP server {imap_host}:{imap_port}."
        ) from exc
    try:
        client.login(address, password)
        # Selecting proves the folder exists, which is the other way setup goes
        # wrong: a folder named in the UI that IMAP spells differently.
        typ, _ = client.select(imap_mailbox(folder), readonly=True)
        if typ != "OK":
            raise MailBackendError(
                f"Signed in to {imap_host}, but the folder {folder!r} does not exist "
                "on this mailbox. Check its exact name in your webmail."
            )
        checked["imap"] = "ok"
    except imaplib.IMAP4.error as exc:
        log.warning("IMAP login to %s refused: %s", imap_host, type(exc).__name__)
        raise MailBackendNotConfigured(
            f"{imap_host} refused the app password for {address}. Note that providers "
            "want an application password here, not the one you use on their website."
        ) from exc
    except OSError as exc:
        log.warning("IMAP session with %s failed: %s", imap_host, type(exc).__name__)
        raise MailBackendUnavailable(
            f"The IMAP server {imap_host} dropped the connection or timed out."
        ) from exc
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):  # pragma: no cover - best effort
            pass

    try:
        with _smtp_client(smtp_host, smtp_port, timeout=PROBE_TIMEOUT) as smtp:
            smtp.login(address, password)
            checked["smtp"] = "ok"
    except smtplib.SMTPAuthenticationError as exc:
        log.warning("SMTP login to %s refused: %s", smtp_host, type(exc).__name__)
        raise MailBackendNotConfigured(
            f"IMAP accepted the password but {smtp_host} refused it. Check "
            "that outgoing (SMTP) access is enabled for this mailbox."
        ) from exc
    except ssl.SSLError as exc:
        log.warning("SMTP TLS with %s failed: %s", smtp_host, type(exc).__name__)
        raise MailBackendUnavailable(_tls_failed(smtp_host)) from exc
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("SMTP connect to %s failed: %s", smtp_host, type(exc).__name__)
        raise MailBackendUnavailable(
            f"Could not reach the SMTP server {smtp_host}:{smtp_port}, or it would not "
            "start TLS."
        ) from exc

    return checked


def connect(
    job_seeker_id: str,
    *,
    address: str,
    password: str,
    provider: str | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    folder: str = "INBOX",
    verify: bool = True,
) -> dict:
    """Verify an app password, then store the mailbox.  Returns the account row.

    ``verify=False`` exists for the tests and for a deliberate offline setup; it
    is not exposed on the API, because storing a credential nobody has checked
    is how a mailbox ends up silently not polling.  The host check is not
    skipped by it: what is stored is what the poller will later connect to.
    """
    address = _clean(address, "address").lower()
    if not _ADDRESS_RE.match(address):
        raise MailBackendError("That is not an e-mail address.")
    if not password.strip():
        raise MailBackendError("An application password is required.")
    if any(ch in "\r\n\0" for ch in password):
        raise MailBackendError("The application password contains a character that is not allowed.")
    folder = _clean(folder, "folder name") or "INBOX"
    if len(folder) > 200:
        raise MailBackendError("The folder name is too long.")

    settings = resolve_provider(
        provider,
        imap_host=imap_host,
        smtp_host=smtp_host,
        imap_port=imap_port,
        smtp_port=smtp_port,
    )
    check_endpoint(settings["imap_host"], settings["imap_port"], kind="imap")
    check_endpoint(settings["smtp_host"], settings["smtp_port"], kind="smtp")
    checked = verify_credentials(address, password, settings, folder=folder) if verify else {}

    credentials = {**settings, "password": password, "folder": folder}
    account_id = repo.upsert_account(
        job_seeker_id,
        backend=BACKEND_KEY,
        address=address,
        credentials_enc=encrypt_credentials(credentials, job_seeker_id, what="IMAP mailbox"),
        # No OAuth scopes exist for this backend.  The column is left null
        # rather than filled with something scope-shaped, so that the IMAP-scope
        # test in mail.inbox cannot accidentally match on it.
        scopes=None,
    )
    # Seed the poll cursor with the folder, so the first poll reads the right
    # one.  A different folder from last time drops the cursor: UIDs are only
    # meaningful within one folder, and carrying the old last_uid over would
    # skip every message in the new folder below it.
    state = repo.get_poll_state(account_id) or {}
    if state.get("folder") != folder:
        repo.save_poll_state(
            account_id,
            {
                "folder": folder,
                "last_uid": None,
                "uid_validity": None,
                "last_polled_at": state.get("last_polled_at"),
            },
        )

    log.info("Connected IMAP mailbox (%s) for seeker %s", settings["provider"], job_seeker_id)
    return {
        "id": account_id,
        "job_seeker_id": job_seeker_id,
        "backend": BACKEND_KEY,
        "address": address,
        "folder": folder,
        "verified": checked,
        **settings,
    }


def disconnect(account: dict) -> dict:
    """Forget the password.

    There is no provider-side revocation to call: an application password is
    revoked by the job seeker on the provider's own security page, and the
    result says so, so the UI can tell them that deleting it here is only half
    the job.
    """
    provider = ""
    try:
        provider = str(
            decrypt_credentials(
                account.get("credentials_enc"), str(account["job_seeker_id"]), what="IMAP mailbox"
            ).get("provider", "")
        )
    except MailBackendError:  # pragma: no cover - the blob may already be gone
        pass
    repo.clear_credentials(account["id"])
    url = str(PROVIDERS.get(provider, {}).get("app_password_url", "")) if provider else ""
    return {
        "revoked_locally": True,
        "revoked_at_provider": False,
        "provider": provider,
        "revoke_hint": (
            f"Dream Job has forgotten this password. Revoke it at the provider too: {url}"
            if url
            else "Dream Job has forgotten this password. Revoke it on your provider's "
            "security page as well."
        ),
    }


def sending_is_armed() -> bool:
    """Whether a send would actually leave the machine (RK-05).

    Exposed for the connect screen: a job seeker who has just connected their
    own mailbox should be told plainly that dry-run is still on, rather than
    concluding from a successful connection that mail is now going out.
    """
    return not get_settings().mail_dry_run
