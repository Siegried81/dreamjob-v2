"""Parse XML that came from a third party without trusting it.

The standard library parser expands entities, so a feed can ask for gigabytes
out of a few hundred bytes (a "billion laughs" document) or have the parser
read a local file (an external entity).  Every feed, sitemap and RSS document
the adapters fetch comes from a site we do not control, so they all go through
``defusedxml``, which refuses both.

A refused document is reported as ``ElementTree.ParseError``, the error the
callers already handle for a malformed feed, so no adapter needs a second
``except`` and a hostile document reads as a broken one.
"""

from __future__ import annotations

from xml.etree import ElementTree

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as _safe


def fromstring(text: str | bytes) -> ElementTree.Element:
    """``ElementTree.fromstring`` that refuses entity expansion and external entities."""
    try:
        return _safe.fromstring(text)
    except DefusedXmlException as exc:
        raise ElementTree.ParseError(f"refused XML: {exc}") from exc
