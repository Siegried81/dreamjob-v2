"""The app-password IMAP/SMTP backend (FR-325, FR-326, NFR-204).

Everything here runs against a throwaway SQLite file with no network: the IMAP
and SMTP servers are fakes substituted at the two seams the backend leaves for
exactly that (``imap_basic._imap_client`` / ``_smtp_client``, and
``inbox.imaplib``), and ``socket.getaddrinfo`` answers every name with one
public address unless a test says otherwise, so the login probe, the UID
cursor, the host check and the credential envelope are exercised for real while
nothing opens a socket or asks DNS.

What these tests are pinning down, beyond "it works":

* the credential is encrypted at rest, and the plaintext password appears
  nowhere in the row (NFR-204);
* a mailbox is not stored when the provider refused it, because a credential
  that only fails at the next poll makes a mailbox that looks connected and
  reads nothing;
* the poller is driven by the backend's *capabilities*, so an app-password
  mailbox is polled and the webhook relay is skipped with a reason - this is
  the gate that previously read ``backend != "gmail_oauth"`` and silently
  dropped every other mailbox;
* reading never writes: the IMAP session is opened read-only and a message
  matching no dispatch becomes an ``incoming_reply``, never an opportunity;
* the server only connects where it should: every socket verifies TLS, and a
  host resolving to a loopback, private, link-local or metadata address - or a
  port outside the mail ones - is refused at connect, poll and send;
* nothing the client, the database or the log sees carries the password or
  the server's own error text.
"""

from __future__ import annotations

import base64
import email.utils
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_MAIL_DRY_RUN",
)

ADDRESS = "seeker@yahoo.fr"
APP_PASSWORD = "abcdefghijklmnop"  # the 16-character shape Yahoo issues


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    # Pinned to the shipped default (RK-05): dry_run.install_transport_guard()
    # wraps every backend in dreamjob.mail for the whole process, so whether the
    # guard fires here must not depend on which test file ran first.
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
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
def seeker_id() -> str:
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    return insert_row(
        "job_seeker",
        {
            "email": ADDRESS,
            "display_name": "Stephane van der Aa",
            "locale": "fr",
            "created_at": now,
            "updated_at": now,
        },
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _raw_message(
    *, subject: str, from_address: str, to: str = ADDRESS, message_id: str = "", body: str = "hello"
) -> bytes:
    """One RFC 5322 message as a server would hand it back."""
    mid = message_id or email.utils.make_msgid(domain="example.test")
    return (
        f"From: {from_address}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {mid}\r\n"
        f"Date: {email.utils.formatdate()}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        f"{body}\r\n"
    ).encode()


class FakeIMAP:
    """Just enough IMAP4_SSL for the poller: login, select, status, uid, logout.

    Records what it was asked so the tests can assert on the two things that
    matter and are invisible in the return value: that the session was opened
    read-only, and that the folder selected was the one the mailbox was
    connected with.
    """

    #: Set per test before the class is used as a constructor.
    password: str | None = APP_PASSWORD
    messages: dict[int, bytes] = {}
    #: RFC822.SIZE overrides per UID, to exercise the size cap without 25 MB.
    sizes: dict[int, int] = {}
    uid_validity: str = "1000"
    folders: tuple[str, ...] = ("INBOX",)

    instances: list[FakeIMAP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None, ssl_context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.logged_in_as: str | None = None
        self.selected: str | None = None
        self.readonly: bool | None = None
        self.xoauth2_used = False
        self.logged_out = False
        self.fetched: list[int] = []
        FakeIMAP.instances.append(self)

    # -- auth -------------------------------------------------------------
    def login(self, user: str, password: str):
        import imaplib

        if FakeIMAP.password is None or password != FakeIMAP.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")
        self.logged_in_as = user
        return ("OK", [b"LOGIN completed"])

    def authenticate(self, mechanism: str, authobject):
        self.xoauth2_used = True
        authobject(b"")
        return ("OK", [b"AUTH completed"])

    # -- folders ----------------------------------------------------------
    @staticmethod
    def _mailbox(arg: str) -> str | None:
        """The one mailbox a server reads from imaplib's verbatim argument.

        imaplib sends the argument as-is, so an unquoted name with a space is
        two arguments to a real server: ``None`` stands for that BAD command.
        """
        if len(arg) >= 2 and arg[0] == arg[-1] == '"':
            return arg[1:-1]
        return None if " " in arg else arg

    def select(self, folder: str = "INBOX", readonly: bool = False):
        self.selected_arg = folder
        name = self._mailbox(folder)
        if name is None:
            return ("BAD", [b"Command Argument Error. 11"])
        if name not in FakeIMAP.folders:
            return ("NO", [b"no such mailbox"])
        self.selected = name
        self.readonly = readonly
        return ("OK", [str(len(FakeIMAP.messages)).encode()])

    def status(self, folder: str, what: str):
        name = self._mailbox(folder)
        return ("OK", [f'"{name}" (UIDVALIDITY {FakeIMAP.uid_validity})'.encode()])

    # -- reading ----------------------------------------------------------
    def uid(self, command: str, *args):
        if command.upper() == "SEARCH":
            # args is (None, "UID <low>:*") as imaplib's signature wants.
            spec = args[-1]
            low = int(str(spec).split()[1].split(":")[0])
            hits = sorted(u for u in FakeIMAP.messages if u >= low)
            return ("OK", [" ".join(str(u) for u in hits).encode()])
        if command.upper() == "FETCH" and args[1] == "(RFC822.SIZE)":
            uid = int(args[0])
            size = FakeIMAP.sizes.get(uid, len(FakeIMAP.messages[uid]))
            return ("OK", [f"{uid} (UID {uid} RFC822.SIZE {size})".encode()])
        if command.upper() == "FETCH":
            uid = int(args[0])
            self.fetched.append(uid)
            raw = FakeIMAP.messages[uid]
            return ("OK", [(f"{uid} (RFC822 {{{len(raw)}}}".encode(), raw), b")"])
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"logging out"])


class FakeSMTP:
    """Context-managed stand-in for smtplib.SMTP_SSL."""

    password: str | None = APP_PASSWORD
    sent: list = []
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.logged_in_as: str | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user: str, password: str):
        import smtplib

        if FakeSMTP.password is None or password != FakeSMTP.password:
            raise smtplib.SMTPAuthenticationError(535, b"Invalid credentials")
        self.logged_in_as = user

    def send_message(self, mime):
        FakeSMTP.sent.append(mime)


PUBLIC_IP = "17.253.144.10"
REAL_CLIENTS: dict[str, object] = {}


def _getaddrinfo_returning(*addresses: str):
    """A ``socket.getaddrinfo`` stand-in answering every name with ``addresses``."""
    import socket

    def fake(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    return fake


@pytest.fixture(autouse=True)
def fakes(monkeypatch) -> Iterator[None]:
    """Substitute both servers, and reset the class-level state between tests."""
    from dreamjob.mail import imap_basic

    FakeIMAP.instances = []
    FakeIMAP.password = APP_PASSWORD
    FakeIMAP.uid_validity = "1000"
    FakeIMAP.folders = ("INBOX",)
    FakeIMAP.messages = {}
    FakeIMAP.sizes = {}
    FakeSMTP.instances = []
    FakeSMTP.password = APP_PASSWORD
    FakeSMTP.sent = []

    # The real client factories, kept for the TLS tests that inspect them.
    REAL_CLIENTS["imap"] = imap_basic._imap_client
    REAL_CLIENTS["smtp"] = imap_basic._smtp_client
    # No real DNS: every name resolves to one public address unless a test
    # says otherwise.
    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_returning(PUBLIC_IP))
    monkeypatch.setattr(
        imap_basic, "_imap_client", lambda host, port, *, timeout: FakeIMAP(host, port, timeout)
    )
    monkeypatch.setattr(
        imap_basic, "_smtp_client", lambda host, port, *, timeout: FakeSMTP(host, port, timeout)
    )
    # The poller constructs imaplib.IMAP4_SSL directly; swap the symbol it holds.
    from dreamjob.mail import inbox

    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", FakeIMAP)
    yield


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------


def test_yahoo_preset_fills_both_hosts():
    from dreamjob.mail.imap_basic import resolve_provider

    settings = resolve_provider("yahoo")
    assert settings["imap_host"] == "imap.mail.yahoo.com"
    assert settings["smtp_host"] == "smtp.mail.yahoo.com"
    # Implicit TLS on both, so no STARTTLS upgrade can silently not happen.
    assert (settings["imap_port"], settings["smtp_port"]) == (993, 465)


def test_explicit_hosts_win_over_the_preset():
    """A self-hosted server needs no entry in PROVIDERS."""
    from dreamjob.mail.imap_basic import resolve_provider

    settings = resolve_provider(None, imap_host="mail.example.test", smtp_host="mail.example.test")
    assert settings["provider"] == "custom"
    assert settings["imap_host"] == "mail.example.test"


def test_unknown_provider_is_refused_by_name():
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import resolve_provider

    with pytest.raises(MailBackendError, match="Unknown mail provider"):
        resolve_provider("yahou")


def test_a_provider_with_no_hosts_at_all_is_refused():
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import resolve_provider

    with pytest.raises(MailBackendError, match="IMAP host and an SMTP host"):
        resolve_provider(None)


# ---------------------------------------------------------------------------
# Connecting (NFR-204)
# ---------------------------------------------------------------------------


def test_connect_verifies_both_halves_then_stores(seeker_id: str):
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")

    assert account["backend"] == "imap_basic"
    assert account["address"] == ADDRESS
    assert account["verified"] == {"imap": "ok", "smtp": "ok"}
    # Both halves were actually probed - providers refuse them separately.
    assert FakeIMAP.instances[0].logged_in_as == ADDRESS
    assert FakeSMTP.instances[0].logged_in_as == ADDRESS
    # The probe does not hold the session open.
    assert FakeIMAP.instances[0].logged_out is True
    # And it looks, never touches.
    assert FakeIMAP.instances[0].readonly is True


def test_the_password_is_encrypted_at_rest_and_not_in_the_row(seeker_id: str):
    """NFR-204: the credential exists only inside the envelope."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    row = repo.get_account_any(account["id"])

    blob = bytes(row["credentials_enc"])
    assert APP_PASSWORD.encode() not in blob
    assert APP_PASSWORD not in repr(dict(row))
    # No OAuth scopes are invented for a backend that has none; the IMAP-scope
    # test in the poller must not be able to match on this column.
    assert not row["scopes"]

    # It round-trips through the backend, and only there.
    from dreamjob.mail.base import get_backend

    backend = get_backend("imap_basic", row)
    assert backend.imap_password() == APP_PASSWORD
    assert backend.imap_endpoint() == ("imap.mail.yahoo.com", 993)


def test_a_refused_password_stores_nothing(seeker_id: str):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.base import MailBackendNotConfigured
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.password = "something-else"
    with pytest.raises(MailBackendNotConfigured, match="application password"):
        connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")

    assert repo.accounts_to_poll(["imap_basic"]) == []


def test_imap_accepting_while_smtp_refuses_says_which_half(seeker_id: str):
    """Reporting "wrong password" here would send someone to regenerate a good one."""
    from dreamjob.mail.base import MailBackendNotConfigured
    from dreamjob.mail.imap_basic import connect

    FakeSMTP.password = "something-else"
    with pytest.raises(MailBackendNotConfigured, match="IMAP accepted the password but"):
        connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")


def test_a_folder_that_does_not_exist_is_caught_at_connect(seeker_id: str):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="does not exist"):
        connect(
            seeker_id,
            address=ADDRESS,
            password=APP_PASSWORD,
            provider="yahoo",
            folder="lkdn-glassdoor",
        )


def test_connect_rejects_a_non_address(seeker_id: str):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="not an e-mail address"):
        connect(seeker_id, address="seeker", password=APP_PASSWORD, provider="yahoo")


def test_reconnecting_replaces_the_credential_rather_than_adding_a_row(seeker_id: str):
    """UNIQUE (job_seeker_id, backend, address): a stale password must not linger."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    first = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    FakeIMAP.password = FakeSMTP.password = "rotated-password"
    second = connect(seeker_id, address=ADDRESS, password="rotated-password", provider="yahoo")

    assert first["id"] == second["id"]
    assert len(repo.accounts_to_poll(["imap_basic"])) == 1
    from dreamjob.mail.base import get_backend

    row = repo.get_account_any(second["id"])
    assert get_backend("imap_basic", row).imap_password() == "rotated-password"


# ---------------------------------------------------------------------------
# Polling (FR-326) - the gate this change lifted
# ---------------------------------------------------------------------------


@pytest.fixture
def connected(seeker_id: str) -> dict:
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    return repo.get_account_any(account["id"])


def test_poll_all_now_reads_an_app_password_mailbox(connected: dict, seeker_id: str):
    """The previous gate was ``backend != "gmail_oauth"``; this is the regression."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        11: _raw_message(subject="Re: your application", from_address="recruiter@acme.test"),
    }
    results = inbox.poll_all(seeker_id)

    assert len(results) == 1
    assert results[0]["transport"] == "imap"
    assert results[0]["polled"] == 1
    assert "skipped" not in results[0]


def test_polling_uses_the_password_not_xoauth2(connected: dict):
    from dreamjob.mail import inbox

    FakeIMAP.messages = {7: _raw_message(subject="hi", from_address="recruiter@acme.test")}
    inbox.poll_account(connected)

    client = FakeIMAP.instances[-1]
    assert client.logged_in_as == ADDRESS
    assert client.xoauth2_used is False
    # Per-account endpoint, not the global Gmail default.
    assert (client.host, client.port) == ("imap.mail.yahoo.com", 993)


def test_polling_opens_the_mailbox_read_only(connected: dict):
    """Nothing in Dream Job deletes or moves mail; the session cannot."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {7: _raw_message(subject="hi", from_address="recruiter@acme.test")}
    inbox.poll_account(connected)

    assert FakeIMAP.instances[-1].readonly is True


def test_the_cursor_advances_and_does_not_re_read(connected: dict):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        4: _raw_message(subject="first", from_address="a@acme.test"),
        5: _raw_message(subject="second", from_address="b@acme.test"),
    }
    first = inbox.poll_account(connected)
    assert first["polled"] == 2
    assert repo.get_poll_state(connected["id"])["last_uid"] == 5

    # A second pass with nothing new reads nothing.
    second = inbox.poll_account(connected)
    assert second["polled"] == 0

    FakeIMAP.messages[6] = _raw_message(subject="third", from_address="c@acme.test")
    third = inbox.poll_account(connected)
    assert third["polled"] == 1
    assert FakeIMAP.instances[-1].fetched == [6]


def test_a_changed_uidvalidity_restarts_the_cursor(connected: dict):
    """Stored UIDs mean nothing after the server reissues them."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.messages = {4: _raw_message(subject="first", from_address="a@acme.test")}
    inbox.poll_account(connected)
    assert repo.get_poll_state(connected["id"])["last_uid"] == 4

    FakeIMAP.uid_validity = "2000"
    FakeIMAP.messages = {1: _raw_message(subject="renumbered", from_address="d@acme.test")}
    result = inbox.poll_account(connected)

    assert result["polled"] == 1
    assert repo.get_poll_state(connected["id"])["uid_validity"] == "2000"


def test_the_mailbox_own_messages_are_skipped(connected: dict):
    """Otherwise every sent message would be read back as an incoming one."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        8: _raw_message(subject="mine", from_address=ADDRESS),
        9: _raw_message(subject="theirs", from_address="recruiter@acme.test"),
    }
    assert inbox.poll_account(connected)["polled"] == 1


def test_the_connected_folder_is_the_one_read(seeker_id: str):
    """Connecting with a sub-folder seeds the cursor, so the first poll reads it."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.folders = ("INBOX", "lkdn-glassdoor")
    account = connect(
        seeker_id,
        address=ADDRESS,
        password=APP_PASSWORD,
        provider="yahoo",
        folder="lkdn-glassdoor",
    )
    FakeIMAP.messages = {3: _raw_message(subject="alert", from_address="jobs@linkedin.test")}
    inbox.poll_account(repo.get_account_any(account["id"]))

    assert FakeIMAP.instances[-1].selected == "lkdn-glassdoor"


def test_an_unmatched_message_becomes_a_reply_row_never_an_opportunity(connected: dict):
    """A job alert is recorded and classified; it is not a source of vacancies."""
    from dreamjob.db.connection import query_all
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        2: _raw_message(
            subject="Jobs for you", from_address="jobs-listings@linkedin.test", body="5 new jobs"
        )
    }
    inbox.poll_account(connected)

    replies = query_all("SELECT * FROM incoming_reply", ())
    assert len(replies) == 1
    # It matched no dispatch Dream Job sent, so it hangs off nothing.
    assert replies[0]["dispatch_id"] is None
    assert query_all("SELECT * FROM opportunity", ()) == []


def test_a_failing_mailbox_is_recorded_and_does_not_raise(connected: dict):
    """One bad mailbox must not stop a run that has others to poll."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.password = "rotated-at-the-provider"
    result = inbox.poll_account(connected)

    assert result["polled"] == 0
    assert "error" in result
    assert repo.get_poll_state(connected["id"])["last_error"]


def test_a_webhook_backend_is_skipped_with_a_reason(seeker_id: str):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    account_id = repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    result = inbox.poll_account(repo.get_account_any(account_id))

    assert result["polled"] == 0
    assert "no mailbox to poll" in result["skipped"]


def test_poll_all_covers_both_personal_backends_on_one_pass(seeker_id: str, connected: dict):
    """A job seeker with a Gmail and a Yahoo gets both read, and the relay skipped."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    repo.upsert_account(
        seeker_id, backend="gmail_oauth", address="seeker@gmail.test", credentials_enc=b"x"
    )
    polled = {a["backend"] for a in repo.accounts_to_poll(inbox.pollable_backends())}

    assert polled == {"imap_basic", "gmail_oauth"}


# ---------------------------------------------------------------------------
# Sending (FR-325)
# ---------------------------------------------------------------------------


@pytest.fixture
def armed_transport() -> Iterator[None]:
    """Disarm the dry run, for the tests that mean to reach the transport.

    The guard reads the setting at call time, so flipping it needs no
    re-registration - which is exactly what makes this fixture enough.
    """
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "false"
    get_settings.cache_clear()
    yield
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
    get_settings.cache_clear()


def test_the_dry_run_guard_covers_this_backend(connected: dict):
    """RK-05: a new transport must not be a way around the dry run.

    The guard wraps every backend class defined in ``dreamjob.mail``, so this
    one is covered by being there - but that is worth asserting, because the
    cost of being wrong is a message leaving the machine unintentionally.
    """
    from dreamjob.mail.base import get_backend
    from dreamjob.mail.dry_run import TransportBlocked, guarded_backends

    assert "imap_basic" in guarded_backends()
    with pytest.raises(TransportBlocked):
        get_backend("imap_basic", connected).send(_message())
    # Nothing reached the provider.
    assert FakeSMTP.sent == []


def _message() -> object:
    from dreamjob.mail.base import OutgoingMessage

    return OutgoingMessage(
        to_email="recruiter@acme.test",
        subject="Introduction",
        body_text="Bonjour,\n\nStephane",
        message_id="<composed@dreamjob.test>",
    )


def test_send_goes_out_over_smtp_as_the_job_seeker(connected: dict, armed_transport: None):
    from dreamjob.mail.base import get_backend

    result = get_backend("imap_basic", connected).send(_message())

    assert result.ok
    assert result.backend == "imap_basic"
    # SMTP does not rewrite the id, so the one in the send log is the one that
    # will come back in a reply's References (FR-326).
    assert result.message_id == "<composed@dreamjob.test>"
    assert FakeSMTP.instances[-1].logged_in_as == ADDRESS
    assert (
        FakeSMTP.sent[-1]["From"].endswith(f"<{ADDRESS}>") or ADDRESS in FakeSMTP.sent[-1]["From"]
    )


def test_a_rotated_password_reports_setup_not_a_transport_failure(
    connected: dict, armed_transport: None
):
    from dreamjob.mail.base import MailBackendNotConfigured, get_backend

    FakeSMTP.password = "rotated-at-the-provider"
    with pytest.raises(MailBackendNotConfigured, match="refused the app password"):
        get_backend("imap_basic", connected).send(_message())


def test_status_reports_the_mailbox_without_the_password(connected: dict):
    from dreamjob.mail.base import get_backend

    status = get_backend("imap_basic", connected).status()

    assert status["configured"] is True
    assert status["address"] == ADDRESS
    assert status["imap_host"] == "imap.mail.yahoo.com"
    assert APP_PASSWORD not in repr(status)


def test_status_of_an_unconnected_backend_points_at_the_connect_route():
    from dreamjob.mail.base import get_backend

    status = get_backend("imap_basic", None).status()

    assert status["configured"] is False
    assert status["connect_path"] == "/api/mail/imap/connect"
    assert "yahoo" in status["providers"]


def test_disconnect_forgets_the_password_and_says_to_revoke_it_too(connected: dict):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import disconnect

    result = disconnect(connected)

    assert result["revoked_locally"] is True
    # There is no provider endpoint to call, and the result must not pretend.
    assert result["revoked_at_provider"] is False
    assert "login.yahoo.com" in result["revoke_hint"]
    assert repo.get_account_any(connected["id"])["credentials_enc"] is None


# ---------------------------------------------------------------------------
# Capability wiring
# ---------------------------------------------------------------------------


def test_the_backend_is_registered_and_declares_itself_pollable():
    from dreamjob.mail.base import backend_class, pollable_backends

    assert "imap_basic" in pollable_backends()
    capabilities = backend_class("imap_basic").capabilities
    # Section 2.4: replies land in the job seeker's own inbox, same as Gmail.
    assert capabilities.replies_to_seeker_inbox is True
    assert capabilities.requires_oauth is False


def test_a_personal_mailbox_outranks_the_relay_for_sending(seeker_id: str, connected: dict):
    from dreamjob.db.repositories import dispatch as repo

    repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    assert repo.active_account(seeker_id)["backend"] == "imap_basic"


# ---------------------------------------------------------------------------
# TLS: every socket checks the certificate and the host name
# ---------------------------------------------------------------------------


def _assert_verifying(context) -> None:
    import ssl

    assert context is not None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


class _StopAtTls(Exception):
    """Raised where the TLS handshake would start: far enough to see everything."""


class _RecordingContext:
    """The real verifying context, recording what it was asked to wrap."""

    def __init__(self):
        from dreamjob.mail.tls import verified_context

        self.real = verified_context()
        self.server_hostnames: list[str | None] = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def wrap_socket(self, sock, server_hostname=None, **kwargs):
        self.server_hostnames.append(server_hostname)
        raise _StopAtTls


@pytest.fixture
def no_socket(monkeypatch):
    """Record where a socket would have been opened, and hand back a dummy one."""
    import socket

    opened: list[tuple] = []

    def fake_create_connection(address, timeout=None, *args, **kwargs):
        opened.append((address, timeout))
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return opened


@pytest.fixture
def recording_context(monkeypatch):
    from dreamjob.mail import imap_basic

    context = _RecordingContext()
    monkeypatch.setattr(imap_basic, "verified_context", lambda: context)
    return context


def test_the_imap_probe_passes_a_verifying_context(no_socket, recording_context):
    """imaplib's own default verifies nothing; the backend must not rely on it."""
    with pytest.raises(_StopAtTls):
        REAL_CLIENTS["imap"]("imap.mail.yahoo.com", 993, timeout=5.0)

    _assert_verifying(recording_context)
    assert recording_context.server_hostnames == ["imap.mail.yahoo.com"]
    assert no_socket == [((PUBLIC_IP, 993), 5.0)]


def test_smtp_on_465_passes_a_verifying_context(no_socket, recording_context):
    with pytest.raises(_StopAtTls):
        REAL_CLIENTS["smtp"]("smtp.mail.yahoo.com", 465, timeout=5.0)

    _assert_verifying(recording_context)
    assert recording_context.server_hostnames == ["smtp.mail.yahoo.com"]
    assert no_socket == [((PUBLIC_IP, 465), 5.0)]


def test_smtp_on_587_upgrades_with_starttls_on_a_verifying_context(monkeypatch):
    from dreamjob.mail import imap_basic

    calls: list = []

    class FakePlainSMTP:
        def __init__(self, host, port, *, address, timeout=None):
            calls.append(("connect", host, port, address, timeout))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, *, context=None):
            calls.append(("starttls", context))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(imap_basic, "_PinnedSMTP", FakePlainSMTP)
    REAL_CLIENTS["smtp"]("smtp.mail.me.com", 587, timeout=5.0)

    assert calls[0] == ("connect", "smtp.mail.me.com", 587, PUBLIC_IP, 5.0)
    starttls = [c for c in calls if c[0] == "starttls"]
    assert len(starttls) == 1
    _assert_verifying(starttls[0][1])


def test_smtp_on_587_without_starttls_does_not_continue_in_plaintext(monkeypatch):
    import smtplib

    from dreamjob.mail import imap_basic

    closed: list = []

    class NoTlsSMTP:
        def __init__(self, host, port, *, address, timeout=None):
            pass

        def ehlo(self):
            pass

        def starttls(self, *, context=None):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(imap_basic, "_PinnedSMTP", NoTlsSMTP)
    with pytest.raises(smtplib.SMTPNotSupportedError):
        REAL_CLIENTS["smtp"]("smtp.example.test", 587, timeout=5.0)
    assert closed == [True]


def test_the_poll_session_is_the_pinned_client_with_a_timeout(monkeypatch, connected: dict):
    """The poller opens this mailbox through ``_imap_client`` (pinned, verifying).

    Building ``IMAP4_SSL`` from the host name would resolve it again after the
    check; the by-name constructor is made to fail so that path cannot pass.
    """
    from dreamjob.mail import inbox

    def by_name(*args, **kwargs):
        raise AssertionError("the poll connected by name, not to the checked address")

    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", by_name)
    FakeIMAP.messages = {7: _raw_message(subject="hi", from_address="recruiter@acme.test")}
    assert inbox.poll_account(connected)["polled"] == 1
    assert FakeIMAP.instances[-1].timeout == inbox.MAIL_SOCKET_TIMEOUT


# ---------------------------------------------------------------------------
# Hosts the server must not connect to (SSRF)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "fe80::1",
        "fd00:ec2::254",
    ],
)
def test_a_non_public_address_is_refused_at_connect(seeker_id: str, host: str):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="will not connect to"):
        connect(
            seeker_id,
            address=ADDRESS,
            password=APP_PASSWORD,
            imap_host=host,
            smtp_host="smtp.mail.yahoo.com",
        )
    # Refused before any socket was opened, and nothing was stored.
    assert FakeIMAP.instances == []
    assert repo.accounts_to_poll(["imap_basic"]) == []


def test_a_hostname_resolving_to_a_private_address_is_refused(monkeypatch, seeker_id: str):
    from dreamjob.mail import imap_basic
    from dreamjob.mail.base import MailBackendError

    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_returning("10.1.2.3"))
    with pytest.raises(MailBackendError, match="will not connect to"):
        imap_basic.connect(
            seeker_id,
            address=ADDRESS,
            password=APP_PASSWORD,
            imap_host="mail.internal.example",
            smtp_host="mail.internal.example",
        )
    assert FakeIMAP.instances == []


def test_one_private_record_among_public_ones_is_enough_to_refuse(monkeypatch):
    """Every address is checked, not the first: the socket may pick any of them."""
    from dreamjob.mail import imap_basic
    from dreamjob.mail.base import MailBackendError

    monkeypatch.setattr(
        imap_basic.socket, "getaddrinfo", _getaddrinfo_returning(PUBLIC_IP, "192.168.0.10")
    )
    with pytest.raises(MailBackendError, match="will not connect to"):
        imap_basic.check_endpoint("mail.example.test", 993, kind="imap")


def test_the_host_is_re_checked_before_every_poll(monkeypatch, connected: dict):
    """A name whose DNS moved to a private address after connect is not trusted."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import imap_basic, inbox

    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_returning("127.0.0.1"))
    before = len(FakeIMAP.instances)
    result = inbox.poll_account(connected)

    assert result["polled"] == 0
    assert "reconnect" in result["error"]
    assert len(FakeIMAP.instances) == before  # no socket opened
    assert repo.get_poll_state(connected["id"])["last_error"] == result["error"]


def test_the_host_is_re_checked_before_every_send(
    monkeypatch, connected: dict, armed_transport: None
):
    from dreamjob.mail import imap_basic
    from dreamjob.mail.base import MailBackendError, get_backend

    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_returning("169.254.169.254"))
    with pytest.raises(MailBackendError, match="will not connect to"):
        get_backend("imap_basic", connected).send(_message())
    assert FakeSMTP.instances[1:] == []  # only the connect probe ever ran


@pytest.mark.parametrize(("imap_port", "smtp_port"), [(143, 465), (22, 465), (993, 25), (993, 6379)])
def test_ports_outside_the_mail_allowlist_are_refused(seeker_id: str, imap_port, smtp_port):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="is not accepted"):
        connect(
            seeker_id,
            address=ADDRESS,
            password=APP_PASSWORD,
            provider="yahoo",
            imap_port=imap_port,
            smtp_port=smtp_port,
        )


def test_presets_use_starttls_where_the_provider_only_documents_587():
    from dreamjob.mail.imap_basic import resolve_provider

    assert resolve_provider("icloud")["smtp_port"] == 587
    assert resolve_provider("outlook")["smtp_port"] == 587
    assert resolve_provider("gmail")["smtp_port"] == 465


# ---------------------------------------------------------------------------
# Input that could split a protocol line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("address", "seeker@yahoo.fr\r\nRCPT TO:<x@evil.test>"),
        ("address", 'seek"er@yahoo.fr'),
        ("folder", "INBOX\r\nA1 DELETE INBOX"),
        ("folder", 'INBOX" "x'),
        ("folder", "INBOX\0"),
        ("imap_host", "imap.mail.yahoo.com\r\n"),
    ],
)
def test_control_characters_and_quotes_are_refused(seeker_id: str, field: str, value: str):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    kwargs = {"address": ADDRESS, "password": APP_PASSWORD, "provider": "yahoo", field: value}
    if field == "imap_host":
        kwargs["smtp_host"] = "smtp.mail.yahoo.com"
    with pytest.raises(MailBackendError):
        connect(seeker_id, **kwargs)
    assert FakeIMAP.instances == []


def test_a_header_the_composer_rejects_is_a_backend_error_not_a_crash(
    monkeypatch, connected: dict, armed_transport: None
):
    from dreamjob.mail import composer
    from dreamjob.mail.base import MailBackendError, get_backend

    def broken(_message):
        raise ValueError("Header values may not contain linefeed or carriage return characters")

    monkeypatch.setattr(composer, "to_mime", broken)
    with pytest.raises(MailBackendError, match="could not be composed"):
        get_backend("imap_basic", connected).send(_message())
    assert FakeSMTP.sent == []


# ---------------------------------------------------------------------------
# What reaches the client, the database and the log
# ---------------------------------------------------------------------------


def test_server_text_is_not_echoed_back(seeker_id: str, monkeypatch):
    """A provider's own words would make the connect form a probe of any host."""
    import imaplib

    from dreamjob.mail import imap_basic
    from dreamjob.mail.base import MailBackendError

    class Chatty(FakeIMAP):
        def login(self, user, password):
            raise imaplib.IMAP4.error("SECRET-BANNER internal build 4.2.1")

    monkeypatch.setattr(
        imap_basic, "_imap_client", lambda host, port, *, timeout: Chatty(host, port, timeout)
    )
    with pytest.raises(MailBackendError) as info:
        imap_basic.connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    assert "SECRET-BANNER" not in str(info.value)


def test_the_password_never_reaches_a_response_the_log_or_last_error(
    seeker_id: str, caplog, armed_transport: None
):
    import logging

    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.base import MailBackendError, get_backend
    from dreamjob.mail.imap_basic import connect

    caplog.set_level(logging.DEBUG)
    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    row = repo.get_account_any(account["id"])
    texts = [repr(account), repr(get_backend("imap_basic", row).status())]

    # A refused password, at the next poll and at the next send.
    FakeIMAP.password = FakeSMTP.password = "rotated-at-the-provider"
    polled = inbox.poll_account(row)
    texts += [repr(polled), str(repo.get_poll_state(row["id"])["last_error"])]
    with pytest.raises(MailBackendError) as info:
        get_backend("imap_basic", row).send(_message())
    texts.append(str(info.value))
    # And a refused connect.
    with pytest.raises(MailBackendError) as info:
        connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    texts.append(str(info.value))

    for text in [*texts, caplog.text]:
        assert APP_PASSWORD not in text
    # The poll failure is a fixed sentence, not the server's text.
    assert "AUTHENTICATIONFAILED" not in polled["error"]


# ---------------------------------------------------------------------------
# Poller limits
# ---------------------------------------------------------------------------


def test_an_oversized_message_is_skipped_and_the_cursor_moves_past_it(connected: dict):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        4: _raw_message(subject="huge", from_address="a@acme.test"),
        5: _raw_message(subject="small", from_address="b@acme.test"),
    }
    FakeIMAP.sizes = {4: inbox.MAX_FETCH_BYTES + 1}
    result = inbox.poll_account(connected)

    assert result["polled"] == 1
    assert FakeIMAP.instances[-1].fetched == [5]
    assert repo.get_poll_state(connected["id"])["last_uid"] == 5


def test_reconnecting_with_another_folder_resets_the_cursor(seeker_id: str):
    """UIDs only mean something within one folder."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.folders = ("INBOX", "Jobs")
    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    FakeIMAP.messages = {40: _raw_message(subject="old", from_address="a@acme.test")}
    inbox.poll_account(repo.get_account_any(account["id"]))
    assert repo.get_poll_state(account["id"])["last_uid"] == 40

    connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo", folder="Jobs")
    state = repo.get_poll_state(account["id"])
    assert state["folder"] == "Jobs"
    assert not state["last_uid"]
    assert not state["uid_validity"]

    FakeIMAP.messages = {3: _raw_message(subject="in jobs", from_address="b@acme.test")}
    result = inbox.poll_account(repo.get_account_any(account["id"]))
    assert result["polled"] == 1
    assert FakeIMAP.instances[-1].selected == "Jobs"


def test_the_backend_folder_wins_over_a_stale_cursor_and_resets_it(connected: dict):
    """If the cursor still names another folder, the poll restarts rather than skipping."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    repo.save_poll_state(connected["id"], {"folder": "Archive", "last_uid": 900})
    FakeIMAP.messages = {2: _raw_message(subject="hi", from_address="a@acme.test")}
    result = inbox.poll_account(connected)

    assert result["polled"] == 1
    assert FakeIMAP.instances[-1].selected == "INBOX"
    assert repo.get_poll_state(connected["id"])["folder"] == "INBOX"


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


def _app(seeker_id: str | None):
    from dreamjob.api.deps import CurrentSeeker, current_seeker
    from dreamjob.api.routers import mail as mail_router
    from fastapi import FastAPI

    mail_router._imap_connect_attempts._hits.clear()
    app = FastAPI()
    app.include_router(mail_router.router, prefix="/api/mail")
    if seeker_id is not None:
        app.dependency_overrides[current_seeker] = lambda: CurrentSeeker(
            id=seeker_id,
            email=ADDRESS,
            display_name="Stephane van der Aa",
            is_admin=False,
            locale="fr",
        )
    return app


def test_the_imap_routes_require_a_session():
    from fastapi.testclient import TestClient

    with TestClient(_app(None)) as client:
        assert client.get("/api/mail/imap/providers").status_code == 401
        assert (
            client.post(
                "/api/mail/imap/connect",
                json={"address": ADDRESS, "password": APP_PASSWORD, "provider": "yahoo"},
            ).status_code
            == 401
        )


def test_connect_over_the_api_stores_and_never_echoes_the_password(seeker_id: str):
    from fastapi.testclient import TestClient

    with TestClient(_app(seeker_id)) as client:
        response = client.post(
            "/api/mail/imap/connect",
            json={"address": ADDRESS, "password": APP_PASSWORD, "provider": "yahoo"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["backend"] == "imap_basic"
    assert response.json()["sending_armed"] is False
    assert APP_PASSWORD not in response.text


def test_connect_accepts_json_only(seeker_id: str):
    """A form post is what a cross-site page can send without a preflight."""
    from fastapi.testclient import TestClient

    with TestClient(_app(seeker_id)) as client:
        response = client.post(
            "/api/mail/imap/connect",
            data={"address": ADDRESS, "password": APP_PASSWORD, "provider": "yahoo"},
        )
    assert response.status_code == 422
    assert FakeIMAP.instances == []


def test_connect_over_the_api_refuses_a_private_host_with_a_fixed_message(seeker_id: str):
    from fastapi.testclient import TestClient

    with TestClient(_app(seeker_id)) as client:
        response = client.post(
            "/api/mail/imap/connect",
            json={
                "address": ADDRESS,
                "password": APP_PASSWORD,
                "imap_host": "169.254.169.254",
                "smtp_host": "smtp.mail.yahoo.com",
            },
        )
    assert response.status_code == 400
    assert "will not connect to" in response.json()["detail"]
    assert APP_PASSWORD not in response.text


def test_connect_over_the_api_refuses_a_folder_with_a_line_break(seeker_id: str):
    from fastapi.testclient import TestClient

    with TestClient(_app(seeker_id)) as client:
        response = client.post(
            "/api/mail/imap/connect",
            json={
                "address": ADDRESS,
                "password": APP_PASSWORD,
                "provider": "yahoo",
                "folder": "INBOX\r\nA1 LOGOUT",
            },
        )
    assert response.status_code == 422
    assert APP_PASSWORD not in response.text


def test_connect_attempts_are_rate_limited_per_seeker(seeker_id: str):
    from dreamjob.api.routers import mail as mail_router
    from fastapi.testclient import TestClient

    FakeIMAP.password = "something-else"  # every attempt is refused
    body = {"address": ADDRESS, "password": APP_PASSWORD, "provider": "yahoo"}
    with TestClient(_app(seeker_id)) as client:
        codes = [
            client.post("/api/mail/imap/connect", json=body).status_code
            for _ in range(mail_router.IMAP_CONNECT_LIMIT + 1)
        ]
    assert codes[:-1] == [400] * mail_router.IMAP_CONNECT_LIMIT
    assert codes[-1] == 429


# ---------------------------------------------------------------------------
# scripts/where_is_my_data.py
# ---------------------------------------------------------------------------


def _load_where_is_my_data():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "where_is_my_data.py"
    spec = importlib.util.spec_from_file_location("where_is_my_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_where_is_my_data_reads_the_given_db_read_only_and_masks_addresses(
    connected: dict, capsys
):
    import sqlite3

    script = _load_where_is_my_data()
    db_path = Path(os.environ["DREAMJOB_DB_PATH"])

    assert script.main(["--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert f"database: {db_path}" in out
    assert "imap_basic - s***@yahoo.fr" in out
    assert ADDRESS not in out
    assert APP_PASSWORD not in out

    conn = script.open_read_only(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM mail_account")
    conn.close()


# ---------------------------------------------------------------------------
# The socket goes where the check looked (DNS rebinding)
# ---------------------------------------------------------------------------


def _getaddrinfo_rebinding(first: str, then: str):
    """DNS that answers ``first`` once, then ``then``: a rebinding name."""
    import socket

    answers = iter([first])

    def fake(host, port, *args, **kwargs):
        address = next(answers, then)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return fake


@pytest.mark.parametrize(
    ("kind", "host", "port"),
    [("imap", "imap.mail.yahoo.com", 993), ("smtp", "smtp.mail.yahoo.com", 465)],
)
def test_the_socket_is_opened_to_the_checked_address_not_a_second_lookup(
    monkeypatch, no_socket, recording_context, kind, host, port
):
    """A name that turns private after the check still only reaches the checked address.

    The connection is opened to the IP the check approved; resolving the name
    a second time (as imaplib and smtplib do when handed a host name) would
    have reached 10.0.0.7.  The certificate is still checked against the name.
    """
    from dreamjob.mail import imap_basic

    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_rebinding(PUBLIC_IP, "10.0.0.7"))
    with pytest.raises(_StopAtTls):
        REAL_CLIENTS[kind](host, port, timeout=5.0)

    assert no_socket == [((PUBLIC_IP, port), 5.0)]
    assert recording_context.server_hostnames == [host]


def test_starttls_on_587_is_opened_to_the_checked_address(monkeypatch, no_socket):
    """The plain SMTP leg is pinned too; STARTTLS then names the host."""
    import smtplib

    from dreamjob.mail import imap_basic

    monkeypatch.setattr(imap_basic.socket, "getaddrinfo", _getaddrinfo_rebinding(PUBLIC_IP, "10.0.0.7"))
    # Stop right after the socket: the greeting is read from it.
    monkeypatch.setattr(smtplib.SMTP, "getreply", lambda self: (_ for _ in ()).throw(_StopAtTls()))
    with pytest.raises(_StopAtTls):
        REAL_CLIENTS["smtp"]("smtp.mail.me.com", 587, timeout=5.0)

    assert no_socket == [((PUBLIC_IP, 587), 5.0)]


# ---------------------------------------------------------------------------
# Folder names as IMAP arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "argument"),
    [
        ("INBOX", '"INBOX"'),
        ("Sent Items", '"Sent Items"'),
        ("R&D", '"R&-D"'),
        ("Boîte de réception", '"Bo&AO4-te de r&AOk-ception"'),
        # RFC 3501 section 5.1.3's own example.
        ("~peter/mail/台北/日本語", '"~peter/mail/&U,BTFw-/&ZeVnLIqe-"'),
    ],
)
def test_a_folder_name_becomes_one_quoted_modified_utf7_argument(name, argument):
    from dreamjob.mail.base import imap_mailbox

    assert imap_mailbox(name) == argument


@pytest.mark.parametrize("name", ['a"b', "a\\b", "a\r\nb", "a\0b"])
def test_a_folder_name_that_could_break_the_quoting_is_refused(name):
    from dreamjob.mail.base import MailBackendError, imap_mailbox

    with pytest.raises(MailBackendError):
        imap_mailbox(name)


@pytest.mark.parametrize(
    ("folder", "on_server"),
    [("Sent Items", "Sent Items"), ("Boîte de réception", "Bo&AO4-te de r&AOk-ception")],
)
def test_a_folder_with_a_space_or_an_accent_connects_and_is_polled(
    seeker_id: str, folder: str, on_server: str
):
    """``Sent Items`` used to reach the server as two arguments; ``Boîte`` never left imaplib."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.folders = ("INBOX", on_server)
    account = connect(
        seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo", folder=folder
    )
    assert FakeIMAP.instances[-1].selected == on_server

    FakeIMAP.messages = {3: _raw_message(subject="reply", from_address="hr@acme.test")}
    result = inbox.poll_account(repo.get_account_any(account["id"]))

    assert result["polled"] == 1
    assert FakeIMAP.instances[-1].selected_arg == f'"{on_server}"'
    assert repo.get_poll_state(account["id"])["folder"] == folder
