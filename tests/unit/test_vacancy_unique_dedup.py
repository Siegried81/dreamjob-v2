"""One vacancy row per dedup key: migration 160 and the writer around it (FR-184).

An installed database held 242 dedup keys written twice, each pair from the
same adapter.  The writer looks the key up and then inserts, in two
transactions, and the index under the key was a plain one, so a writer that
looked before another one committed stored a second row.  A fuzzy merge could
do the same from the other side: it copies the incoming key onto the matched
row, whether or not another row already holds it.

These tests reproduce both against the writer, then check that migration 160
folds the duplicates an installed database already holds without losing the
opportunities and provenance that point at them.
"""

from __future__ import annotations

import sqlite3

import pytest
from dreamjob.config import get_settings
from dreamjob.db import migrator
from dreamjob.db.connection import insert_row, query_all, query_one, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import dedup, knowledge_base

UNIQUE_MIGRATION = "160"

POSTING = {
    "title": "Data Engineer",
    "company_name_raw": "Zephyr Logistics NV",
    "location": "Gent",
    "posted_at": "2026-10-01",
    "source_url": "https://example.com/jobs/1",
}


def _use_scratch_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A scratch database with every migration applied."""
    _use_scratch_db(tmp_path, monkeypatch)
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def pre_160_db(tmp_path, monkeypatch):
    """A scratch database migrated up to 159, with 160 pending.

    Later migrations are left out while seeding, so the duplicates can be
    written the way an installed database holds them, and put back for the
    ``migrate()`` call the test makes.
    """
    _use_scratch_db(tmp_path, monkeypatch)
    every = migrator.discover()
    monkeypatch.setattr(migrator, "discover", lambda: [m for m in every if m[0] < UNIQUE_MIGRATION])
    migrate()
    monkeypatch.setattr(migrator, "discover", lambda: every)
    yield
    get_settings.cache_clear()


def _vacancies() -> list[dict]:
    return query_all("SELECT * FROM vacancy ORDER BY collected_at")


def _write(posting: dict) -> knowledge_base.WriteOutcome | None:
    return knowledge_base.KnowledgeBaseWriter(adapter_key="board.eures").write(
        {"entity_type": "vacancy", "data": dict(posting), "confidence": 0.8}
    )


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_a_writer_that_lost_the_race_merges_instead_of_inserting(db, monkeypatch):
    """The second writer looked the key up before the first one committed.

    Its lookup and its fuzzy candidates both come back empty, exactly as they
    would inside that window, so the only thing left to stop a second row is
    the database itself.
    """
    first = _write(POSTING)
    real_lookup = knowledge_base.repo.vacancy_by_dedup_key
    blind = {"lookup": True}

    def lookup(key):
        if blind.pop("lookup", False):
            return None
        return real_lookup(key)

    monkeypatch.setattr(knowledge_base.repo, "vacancy_by_dedup_key", lookup)
    monkeypatch.setattr(knowledge_base.repo, "vacancy_candidates", lambda *a, **k: [])

    second = _write({**POSTING, "description": "Spark and Kafka"})

    rows = _vacancies()
    assert len(rows) == 1, "a second row was stored for a key the knowledge base already holds"
    assert second is not None and second.created is False
    assert second.entity_id == first.entity_id
    assert rows[0]["description"] == "Spark and Kafka", "the late record is merged, not dropped"


def test_a_fuzzy_merge_never_copies_a_key_another_row_holds(db, monkeypatch):
    """A fuzzy match carries the incoming key onto the matched row.

    When a second row already owns that key, the copy would leave two rows
    under one key.  The matched row keeps its own key and still takes the merge.
    """
    held = _write(POSTING)
    elsewhere = _write({**POSTING, "location": "Antwerpen", "source_url": "https://example.com/2"})
    incoming = {**POSTING, "location": "Antwerpen", "source_url": "https://example.com/2",
                "description": "Remote two days a week"}
    target = query_one("SELECT * FROM vacancy WHERE id = ?", (held.entity_id,))

    monkeypatch.setattr(knowledge_base.repo, "vacancy_by_dedup_key", lambda key: None)
    monkeypatch.setattr(knowledge_base.repo, "vacancy_candidates", lambda *a, **k: [target])
    monkeypatch.setattr(dedup, "best_vacancy_match", lambda data, rows: (rows[0], 0.95))

    outcome = _write(incoming)

    assert outcome is not None and outcome.entity_id == held.entity_id
    keys = [r["dedup_key"] for r in _vacancies()]
    assert len(keys) == len(set(keys)) == 2, "two rows ended up under one dedup key"
    merged = query_one("SELECT * FROM vacancy WHERE id = ?", (held.entity_id,))
    assert merged["dedup_key"] == target["dedup_key"]
    assert merged["description"] == "Remote two days a week"
    assert elsewhere.entity_id != held.entity_id


def test_the_database_refuses_a_second_row_for_one_key(db):
    """The rule lives in the schema, so no other writer can get around it."""
    insert_row("vacancy", {"title": "A", "dedup_key": "k", "collected_at": utcnow()})
    with pytest.raises(sqlite3.IntegrityError):
        insert_row("vacancy", {"title": "A", "dedup_key": "k", "collected_at": utcnow()})
    insert_row("vacancy", {"title": "B", "collected_at": utcnow()})
    insert_row("vacancy", {"title": "B", "collected_at": utcnow()})  # no key, no identity


# ---------------------------------------------------------------------------
# Migration 160 on a database that already holds duplicates
# ---------------------------------------------------------------------------


def _seeker_and_campaigns(n: int) -> tuple[str, list[str]]:
    seeker = insert_row("job_seeker", {
        "email": "seeker@example.com", "display_name": "Seeker",
        "created_at": utcnow(), "updated_at": utcnow(),
    })
    directives = insert_row("directive_set", {
        "job_seeker_id": seeker, "name": "Gent", "created_at": utcnow(),
    })
    profile = insert_row("profile_version", {
        "job_seeker_id": seeker, "version": 1, "sections": {}, "created_at": utcnow(),
    })
    campaigns = [
        insert_row("campaign", {
            "job_seeker_id": seeker, "directive_set_id": directives,
            "profile_version_id": profile, "name": f"Gent {i}", "created_at": utcnow(),
        })
        for i in range(n)
    ]
    return seeker, campaigns


def _pair(key: str) -> tuple[str, str]:
    """The same posting stored twice: an older row and a newer one."""
    old = insert_row("vacancy", {"title": "Data Engineer", "dedup_key": key,
                                 "collected_at": "2026-10-04T14:22:27+00:00"})
    new = insert_row("vacancy", {"title": "Data Engineer", "dedup_key": key,
                                 "collected_at": "2026-10-04T15:06:53+00:00"})
    return old, new


def _opportunity(seeker: str, campaign: str, vacancy: str, **values) -> str:
    return insert_row("opportunity", {
        "job_seeker_id": seeker, "campaign_id": campaign, "vacancy_id": vacancy,
        "kind": "vacancy", "title": "Data Engineer",
        "created_at": utcnow(), "updated_at": utcnow(), **values,
    })


def test_migration_160_keeps_the_newest_row_of_each_key(pre_160_db):
    old, new = _pair("k1")
    lone = insert_row("vacancy", {"title": "Analyst", "dedup_key": "k2", "collected_at": utcnow()})
    keyless = insert_row("vacancy", {"title": "Analyst", "collected_at": utcnow()})
    insert_row("vacancy", {"title": "Analyst", "collected_at": utcnow()})

    assert migrate() == [UNIQUE_MIGRATION]

    ids = {r["id"] for r in _vacancies()}
    assert new in ids and old not in ids
    assert lone in ids and keyless in ids
    assert len(ids) == 4, "rows without a key are never folded together"


def test_migration_160_moves_what_pointed_at_the_deleted_row(pre_160_db):
    seeker, (moved_in, twin_in, acted_in) = _seeker_and_campaigns(3)
    old, new = _pair("k1")
    moved = _opportunity(seeker, moved_in, old)
    _opportunity(seeker, twin_in, new)
    twin = _opportunity(seeker, twin_in, old)
    _opportunity(seeker, acted_in, new)
    acted = _opportunity(seeker, acted_in, old, user_status="interested")
    provenance = insert_row("provenance", {"entity_type": "vacancy", "entity_id": old,
                                           "adapter_key": "board.eures", "created_at": utcnow()})
    insert_row("embedding", {"entity_type": "vacancy", "entity_id": old, "model": "m", "dim": 1,
                             "vector": [0.1], "created_at": utcnow()})

    assert migrate() == [UNIQUE_MIGRATION]

    def vacancy_of(opportunity_id):
        row = query_one("SELECT vacancy_id FROM opportunity WHERE id = ?", (opportunity_id,))
        return row["vacancy_id"] if row else "deleted"

    assert vacancy_of(moved) == new, "an opportunity follows its posting to the surviving row"
    assert vacancy_of(twin) == "deleted", "an untouched copy of an opportunity is removed"
    assert vacancy_of(acted) is None, "one the job seeker acted on is kept"
    assert query_one("SELECT entity_id FROM provenance WHERE id = ?", (provenance,))["entity_id"] == new
    assert query_one("SELECT COUNT(*) AS n FROM embedding WHERE entity_id = ?", (old,))["n"] == 0
    assert query_one("SELECT COUNT(*) AS n FROM opportunity")["n"] == 4
