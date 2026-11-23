"""Dotted paths into a profile's sections: read, write and blank a value.

Conflict resolution addresses a field as ``experience.1.company``.  These three
walk the same path, so they are tested together and without the personal sample
documents the intake tests need.
"""

from __future__ import annotations

from dreamjob.pipeline.profile_intake import clear_path, get_path, set_path


def _sections() -> dict:
    return {
        "contact": {"email": "a@example.org", "phone": ""},
        "experience": [{"company": "Acme", "title": "Lead"}, {"company": "Borealis"}],
    }


def test_get_path_reads_dicts_and_list_indexes_and_defaults_when_absent():
    sections = _sections()
    assert get_path(sections, "contact.email") == "a@example.org"
    assert get_path(sections, "experience.1.company") == "Borealis"
    assert get_path(sections, "experience.2.company", "none") == "none"
    assert get_path(sections, "experience.x", "none") == "none"
    assert get_path(sections, "contact.email.deeper", "none") == "none"


def test_set_path_writes_an_existing_branch_and_refuses_a_missing_one():
    sections = _sections()
    assert set_path(sections, "experience.0.title", "Head") is True
    assert sections["experience"][0]["title"] == "Head"
    assert set_path(sections, "contact.mobile", "123") is True
    assert set_path(sections, "education.0.school", "X") is False
    assert set_path(sections, "experience.5.title", "X") is False


def test_clear_path_blanks_a_list_slot_and_removes_an_empty_dict_key():
    sections = _sections()
    assert clear_path(sections, "experience.0.title") is True
    assert sections["experience"][0]["title"] is None
    assert clear_path(sections, "contact.phone") is True
    assert "phone" not in sections["contact"]
    assert clear_path(sections, "experience.1") is True
    assert sections["experience"][1] is None
    assert len(sections["experience"]) == 2, "indices other fields point at do not shift"
    assert clear_path(sections, "nowhere.at.all") is False
