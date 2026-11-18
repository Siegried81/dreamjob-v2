"""The purge of stale vacancies that no opportunity uses, and its scheduler task.

What is pinned down here:

* a vacancy goes only when it is both unlinked and unseen past the cutoff -
  a linked one stays at any age, a fresh unlinked one stays too;
* its ``provenance`` and ``embedding`` rows go with it, the kept rows' stay;
* the full-text index forgets it (``vacancy_fts`` follows through a trigger);
* ``dry_run`` counts the same rows and deletes nothing;
* the default window is the staleness policy with a 7-day floor, so a policy
  of 0 days cannot make the scheduled purge delete fresh postings;
* an explicit ``days=0`` clears every unlinked vacancy;
* the scheduler task is daily by default and ``0`` switches it off;
* the script counts by default, and backs up before it deletes.

No network is involved; every test runs on its own migrated database.
"""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.config import Settings, get_settings

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
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


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def seed_campaign() -> dict:
    from dreamjob.db.connection import insert_row, to_json, utcnow

    seeker_id = insert_row(
        "job_seeker",
        {"email": "a@example.com", "display_name": "a", "locale": "en",
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "Default", "created_at": utcnow()}
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1,
         "sections": to_json({"summary": "Data lead"}), "created_at": utcnow()},
    )
    campaign_id = insert_row(
        "campaign",
        {"job_seeker_id": seeker_id, "directive_set_id": directive_id,
         "profile_version_id": profile_id, "name": "Autumn", "status": "running",
         "created_at": utcnow()},
    )
    return {"seeker_id": seeker_id, "campaign_id": campaign_id}


def add_vacancy(title: str, collected_at: str) -> str:
    """A vacancy with one provenance row and one embedding, as collection leaves it."""
    from dreamjob.db.connection import insert_row, utcnow
    from dreamjob.db.repositories import knowledge as repo

    vacancy_id = insert_row(
        "vacancy",
        {"title": title, "description": f"{title} in Ghent.", "company_name_raw": "Acme",
         "country": "BE", "collected_at": collected_at},
    )
    repo.record_provenance("vacancy", vacancy_id, adapter_key="board.test", field_path="title")
    insert_row(
        "embedding",
        {"entity_type": "vacancy", "entity_id": vacancy_id, "model": "test", "dim": 2,
         "vector": b"\x00" * 8, "content_hash": vacancy_id, "created_at": utcnow()},
    )
    return vacancy_id


def link(campaign: dict, vacancy_id: str) -> str:
    from dreamjob.db.connection import insert_row, utcnow

    return insert_row(
        "opportunity",
        {"job_seeker_id": campaign["seeker_id"], "campaign_id": campaign["campaign_id"],
         "vacancy_id": vacancy_id, "kind": "vacancy", "title": "Linked", "country": "BE",
         "language": "en", "created_at": utcnow(), "updated_at": utcnow()},
    )


def count(sql: str, params: tuple = ()) -> int:
    from dreamjob.db.connection import query_one

    return int(next(iter(query_one(sql, params).values())))


def vacancy_ids() -> set[str]:
    from dreamjob.db.connection import query_all

    return {row["id"] for row in query_all("SELECT id FROM vacancy")}


@pytest.fixture
def corpus() -> dict[str, str]:
    """Four vacancies: stale/fresh crossed with linked/unlinked."""
    campaign = seed_campaign()
    ids = {
        "stale_unlinked": add_vacancy("Zeppelinpilot", days_ago(30)),
        "stale_linked": add_vacancy("Stale linked role", days_ago(30)),
        "fresh_unlinked": add_vacancy("Fresh unlinked role", days_ago(1)),
        "fresh_linked": add_vacancy("Fresh linked role", days_ago(1)),
    }
    link(campaign, ids["stale_linked"])
    link(campaign, ids["fresh_linked"])
    return ids


# ---------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------


def test_only_the_stale_unlinked_vacancy_goes(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    result = knowledge_base.purge_unlinked_vacancies()

    assert result["vacancies"] == 1
    assert vacancy_ids() == {corpus["stale_linked"], corpus["fresh_unlinked"],
                             corpus["fresh_linked"]}
    assert count("SELECT COUNT(vacancy_id) FROM opportunity") == 2


def test_provenance_and_embeddings_follow_the_vacancy(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    result = knowledge_base.purge_unlinked_vacancies()

    assert result["provenance"] == 1
    assert result["embeddings"] == 1
    gone = corpus["stale_unlinked"]
    assert count("SELECT COUNT(*) FROM provenance WHERE entity_id = ?", (gone,)) == 0
    assert count("SELECT COUNT(*) FROM embedding WHERE entity_id = ?", (gone,)) == 0
    assert count("SELECT COUNT(*) FROM provenance WHERE entity_type = 'vacancy'") == 3
    assert count("SELECT COUNT(*) FROM embedding WHERE entity_type = 'vacancy'") == 3


def test_search_forgets_the_purged_vacancy(corpus: dict[str, str]) -> None:
    from dreamjob.db.repositories import knowledge as repo
    from dreamjob.pipeline import knowledge_base

    assert repo.count_vacancies("Zeppelinpilot") == 1
    knowledge_base.purge_unlinked_vacancies()
    assert repo.count_vacancies("Zeppelinpilot") == 0
    assert repo.count_vacancies("linked") == 2


def test_dry_run_counts_and_deletes_nothing(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    preview = knowledge_base.purge_unlinked_vacancies(dry_run=True)

    assert (preview["vacancies"], preview["provenance"], preview["embeddings"]) == (1, 1, 1)
    assert preview["dry_run"] is True
    assert len(vacancy_ids()) == 4
    assert knowledge_base.purge_unlinked_vacancies()["vacancies"] == 1


def test_a_zero_day_policy_cannot_reach_fresh_postings(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    result = knowledge_base.purge_unlinked_vacancies(policy={"vacancy": 0})

    assert result["days"] == knowledge_base.PURGE_MIN_DAYS
    assert corpus["fresh_unlinked"] in vacancy_ids()


def test_a_longer_policy_is_honoured(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    result = knowledge_base.purge_unlinked_vacancies(policy={"vacancy": 60})

    assert result["days"] == 60
    assert result["vacancies"] == 0


def test_explicit_zero_days_clears_every_unlinked_vacancy(corpus: dict[str, str]) -> None:
    from dreamjob.pipeline import knowledge_base

    result = knowledge_base.purge_unlinked_vacancies(0)

    assert result["vacancies"] == 2
    assert vacancy_ids() == {corpus["stale_linked"], corpus["fresh_linked"]}


# ---------------------------------------------------------------------------
# Settings and the scheduler task
# ---------------------------------------------------------------------------


def test_the_task_is_daily_by_default() -> None:
    from dreamjob.monitoring import scheduler as scheduler_mod

    assert Settings.model_fields["vacancy_purge_seconds"].default == 86400
    task = next(t for t in scheduler_mod.DEFAULT_TASKS if t.name == "vacancy_purge")
    configured = get_settings().vacancy_purge_seconds
    assert task.interval_seconds == max(60, configured)
    assert task.enabled is (configured > 0)


def test_the_task_purges(corpus: dict[str, str]) -> None:
    import asyncio

    from dreamjob.monitoring import scheduler as scheduler_mod

    result = asyncio.run(scheduler_mod._run_vacancy_purge())

    assert result["vacancies"] == 1
    assert corpus["stale_unlinked"] not in vacancy_ids()


def test_zero_switches_the_task_off(tmp_path: Path) -> None:
    """The task list is built at import time, so a fresh interpreter reads the setting."""
    env = {**os.environ, "DREAMJOB_VACANCY_PURGE_SECONDS": "0",
           "DREAMJOB_DB_PATH": str(tmp_path / "data" / "sub.db"),
           "DREAMJOB_DATA_DIR": str(tmp_path / "data"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "backend")}
    code = (
        "from dreamjob.monitoring import scheduler as s;"
        "t = next(t for t in s.DEFAULT_TASKS if t.name == 'vacancy_purge');"
        "print(t.enabled)"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=120, check=True)
    assert out.stdout.strip().splitlines()[-1] == "False"


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------


def _script(monkeypatch: pytest.MonkeyPatch, *argv: str):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "purge_unlinked_vacancies.py"
    spec = importlib.util.spec_from_file_location("purge_unlinked_vacancies", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["purge_unlinked_vacancies.py", *argv])
    return module


def test_the_script_counts_by_default(
    corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert _script(monkeypatch).main() == 0
    assert "1 vacancies" in capsys.readouterr().out
    assert len(vacancy_ids()) == 4


def test_the_script_backs_up_then_deletes(
    corpus: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    assert _script(monkeypatch, "--days", "0", "--apply").main() == 0

    assert vacancy_ids() == {corpus["stale_linked"], corpus["fresh_linked"]}
    db_path = get_settings().abs_db_path
    backups = list(db_path.parent.glob(f"{db_path.name}.backup-*-before-purge"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM vacancy").fetchone()[0] == 4
