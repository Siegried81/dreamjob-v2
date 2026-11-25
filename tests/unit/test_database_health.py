"""The database check the scheduler runs before a pass.

What is pinned down here:

* ``database_health`` says ``None`` for a sound file and for a file that does
  not exist yet, and returns SQLite's own complaint for a file with a damaged
  page (a real one, written over on disk, not a mock);
* on a corrupt database ``run_due`` runs no task, reports the problem, and
  logs it once at CRITICAL rather than once per tick;
* a passed check is trusted for ``DB_CHECK_SECONDS``, a failed one is not, so
  the tasks resume on the first tick after a repair;
* ``--once`` exits with ``EXIT_DATABASE_CORRUPT`` before the migrator writes.

No network is involved; every test runs on its own migrated database.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import database_health
from dreamjob.monitoring import scheduler as scheduler_mod
from dreamjob.monitoring.scheduler import DB_CHECK_SECONDS, EXIT_DATABASE_CORRUPT, Scheduler, Task

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


def _corrupt_copy(tmp_path: Path) -> Path:
    """A small database whose table spans many pages, with one page overwritten."""
    path = tmp_path / "broken.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO t (body) VALUES (?)", [("x" * 500,) for _ in range(400)])
    conn.commit()
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with path.open("r+b") as fh:
        fh.seek(page_size * 3)
        fh.write(b"\xff" * page_size)
    return path


def _counting_task(calls: list[str]) -> Task:
    return Task("probe", 60, lambda: calls.append("ran") or {"ok": True}, "test probe")


# ---------------------------------------------------------------------------
# database_health
# ---------------------------------------------------------------------------


def test_a_sound_database_passes() -> None:
    assert database_health() is None


def test_a_file_that_does_not_exist_yet_is_not_corrupt(tmp_path: Path) -> None:
    assert database_health(tmp_path / "nowhere.db") is None


def test_a_damaged_page_is_reported(tmp_path: Path) -> None:
    problem = database_health(_corrupt_copy(tmp_path))
    assert problem is not None
    assert problem != "ok"


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


def test_no_task_runs_on_a_corrupt_database_and_the_log_says_so_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scheduler_mod, "database_health", lambda: "Tree 219 page 96939: bad")
    calls: list[str] = []
    sched = Scheduler(tasks=[_counting_task(calls)], jitter_seconds=0)

    with caplog.at_level(logging.CRITICAL, logger=scheduler_mod.__name__):
        first = asyncio.run(sched.run_due())
        second = asyncio.run(sched.run_due())

    assert calls == []
    assert "corrupt" in first["database"]["error"]
    assert "Tree 219" in second["database"]["error"]
    assert sum(r.levelno == logging.CRITICAL for r in caplog.records) == 1
    assert sched.status()["database"]["problem"] == "Tree 219 page 96939: bad"


def test_tasks_resume_on_the_first_tick_after_a_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["malformed", None])
    monkeypatch.setattr(scheduler_mod, "database_health", lambda: next(answers))
    calls: list[str] = []
    sched = Scheduler(tasks=[_counting_task(calls)], jitter_seconds=0)

    asyncio.run(sched.run_due())
    assert calls == []
    asyncio.run(sched.run_due())
    assert calls == ["ran"]
    assert sched.db_problem is None


def test_a_passed_check_is_trusted_for_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    checks: list[int] = []
    monkeypatch.setattr(scheduler_mod, "database_health", lambda: checks.append(1))
    sched = Scheduler(tasks=[], jitter_seconds=0)
    start = datetime.now(UTC)

    asyncio.run(sched.database_ok(start))
    asyncio.run(sched.database_ok(start + timedelta(seconds=DB_CHECK_SECONDS - 1)))
    assert len(checks) == 1
    asyncio.run(sched.database_ok(start + timedelta(seconds=DB_CHECK_SECONDS)))
    assert len(checks) == 2


def test_once_exits_with_the_corrupt_code_before_migrating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_mod, "database_health", lambda: "malformed")
    monkeypatch.setattr(scheduler_mod, "scheduler", Scheduler(tasks=[], jitter_seconds=0))
    migrated: list[int] = []
    import dreamjob.db.migrator as migrator

    monkeypatch.setattr(migrator, "migrate", lambda: migrated.append(1))
    monkeypatch.setattr(sys, "argv", ["dreamjob.monitoring.scheduler", "--once"])

    with pytest.raises(SystemExit) as exit_info:
        scheduler_mod.main()
    assert exit_info.value.code == EXIT_DATABASE_CORRUPT
    assert migrated == []
