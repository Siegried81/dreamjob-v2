"""Feeds are third-party XML: entity expansion and external entities are refused."""

from __future__ import annotations

import time
from xml.etree import ElementTree

import pytest
from dreamjob.adapters.ats.teamtailor import TeamtailorAdapter
from dreamjob.adapters.base import RawRecord
from dreamjob.adapters.news.rss import parse_feed
from dreamjob.pipeline import safe_xml

LAUGHS = (
    '<?xml version="1.0"?><!DOCTYPE lolz ['
    '<!ENTITY lol "lol">'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
    '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">'
    "]><rss><channel><title>&lol4;</title></channel></rss>"
)
EXTERNAL = (
    '<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
    "<rss><channel><title>&secret;</title></channel></rss>"
)
FINE = "<rss><channel><title>Acme</title><item><title>Data lead</title></item></channel></rss>"


@pytest.mark.parametrize("payload", [LAUGHS, EXTERNAL])
def test_an_entity_payload_is_rejected_as_a_parse_error(payload):
    with pytest.raises(ElementTree.ParseError):
        safe_xml.fromstring(payload)


def test_ordinary_xml_still_parses():
    assert safe_xml.fromstring(FINE).find("channel/title").text == "Acme"


@pytest.mark.parametrize("payload", [LAUGHS, EXTERNAL])
def test_a_hostile_news_feed_yields_nothing(payload):
    started = time.monotonic()
    assert parse_feed(payload) == []
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("payload", [LAUGHS, EXTERNAL])
def test_a_hostile_teamtailor_feed_is_refused_before_any_expansion(payload):
    raw = RawRecord(url="https://acme.teamtailor.com/jobs.rss", content=payload.encode())
    with pytest.raises(ElementTree.ParseError):
        TeamtailorAdapter().parse(raw)
