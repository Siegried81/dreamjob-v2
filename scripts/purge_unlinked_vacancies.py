"""Delete the vacancies that no opportunity uses, by hand.

The scheduler's ``vacancy_purge`` task does this once a day for postings unseen
for the staleness window (7 days, the floor).  This script is for the cases the
task does not cover: checking what it would remove, or clearing a backlog with
a shorter window, ``--days 0`` included.

The default run only counts.  ``--apply`` deletes, after backing the database
up next to itself, because the rows do not come back: a source that is no
longer read will never collect them again.  The backup goes through SQLite's
backup API rather than a file copy, so pages still in the WAL are included.

    PYTHONPATH=backend .venv/Scripts/python.exe scripts/purge_unlinked_vacancies.py
    PYTHONPATH=backend .venv/Scripts/python.exe scripts/purge_unlinked_vacancies.py --days 0 --apply
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

from dreamjob.config import get_settings
from dreamjob.db.connection import query_one
from dreamjob.pipeline import knowledge_base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="delete what has gone unseen for this many days "
        f"(default: the vacancy staleness window, at least {knowledge_base.PURGE_MIN_DAYS})",
    )
    parser.add_argument("--apply", action="store_true", help="delete (default: count only)")
    args = parser.parse_args()

    before = int(query_one("SELECT COUNT(*) AS n FROM vacancy")["n"])
    preview = knowledge_base.purge_unlinked_vacancies(args.days, dry_run=True)
    print(f"Vacancies: {before}")
    print(
        f"Unlinked and unseen for {preview['days']} days (since {preview['cutoff']}): "
        f"{preview['vacancies']} vacancies, {preview['provenance']} provenance rows, "
        f"{preview['embeddings']} embeddings"
    )
    if not args.apply:
        print("Count only. Pass --apply to delete.")
        return 0
    if not preview["vacancies"]:
        print("Nothing to delete.")
        return 0

    db_path = get_settings().abs_db_path
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.backup-{stamp}-before-purge")
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    print(f"Backup: {backup}")

    done = knowledge_base.purge_unlinked_vacancies(args.days)
    after = int(query_one("SELECT COUNT(*) AS n FROM vacancy")["n"])
    print(f"Deleted {done['vacancies']} vacancies. Vacancies now: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
