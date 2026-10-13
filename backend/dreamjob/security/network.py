"""Which network addresses the server may connect to on a user's behalf.

One shared guard against server-side request forgery (SSRF): the mail backend
(IMAP/SMTP hosts typed by a user) and the egress HTTP client (URLs taken from
job boards, company sites and redirects) both ask the same question - "is this
host a public internet address?" - and must answer it the same way.
"""

from __future__ import annotations

import ipaddress
import socket


def resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to.  A seam for the tests (no real DNS).

    An IP literal (``[::1]``, ``10.0.0.1``) is returned as is, without a lookup.
    """
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal.split("%", 1)[0])
        return [literal]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_address(address: str) -> bool:
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
