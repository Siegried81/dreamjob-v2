"""The one TLS context every mail connection uses.

``imaplib.IMAP4_SSL`` and ``smtplib.SMTP_SSL`` default to
``ssl._create_stdlib_context()``, which checks neither the certificate chain nor
the host name (``verify_mode`` 0, ``check_hostname`` False). A connection made
that way is encrypted to whoever answers: on an untrusted network the Yahoo app
password and the Gmail OAuth token were sent to any machine able to sit in the
path. Every mail socket takes its context from here instead.
"""
from __future__ import annotations

import ssl

#: Seconds before a mail socket gives up. Without it a stalled server held the
#: scheduled pass for as long as the operating system kept the socket open.
MAIL_SOCKET_TIMEOUT = 60


def verified_context() -> ssl.SSLContext:
    """A context that verifies the server certificate and its host name, TLS 1.2+."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
