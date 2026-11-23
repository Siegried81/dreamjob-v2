"""``scripts/bootstrap.py`` and ``scripts/backfill_scoring.py`` build in no address.

The owner comes from ``--email`` or ``DREAMJOB_OWNER_EMAIL``; without either the
script stops instead of creating an account for someone else's address.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from dreamjob.config import get_settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def bootstrap(monkeypatch):
    monkeypatch.setenv("DREAMJOB_OWNER_EMAIL", "")
    monkeypatch.setenv("DREAMJOB_OWNER_NAME", "")
    get_settings.cache_clear()
    spec = importlib.util.spec_from_file_location("bootstrap_script", ROOT / "scripts" / "bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    get_settings.cache_clear()


def test_without_an_address_the_script_stops(bootstrap):
    with pytest.raises(SystemExit, match="DREAMJOB_OWNER_EMAIL"):
        bootstrap.owner_identity(None, None)


def test_the_flags_win_and_the_environment_fills_the_rest(bootstrap, monkeypatch):
    assert bootstrap.owner_identity("a@example.org", "Ana") == ("a@example.org", "Ana")
    monkeypatch.setenv("DREAMJOB_OWNER_EMAIL", "env@example.org")
    monkeypatch.setenv("DREAMJOB_OWNER_NAME", "Env Owner")
    get_settings.cache_clear()
    assert bootstrap.owner_identity(None, None) == ("env@example.org", "Env Owner")
    assert bootstrap.owner_identity(None, "")[1] == "Env Owner"


def test_no_script_or_backend_carries_a_personal_default():
    scanned = [
        ROOT / "scripts" / "bootstrap.py",
        ROOT / "scripts" / "backfill_scoring.py",
        ROOT / "backend" / "dreamjob" / "mail" / "resend_backend.py",
        ROOT / "backend" / "dreamjob" / "mail" / "dispatcher.py",
        ROOT / ".env.example",
    ]
    for path in scanned:
        assert "stepvda.com" not in path.read_text(encoding="utf8"), path.name
