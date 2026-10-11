#!/usr/bin/env python3
"""Where did the rows in this installation come from?  (read-only)

Run it when a count somewhere does not match what you expect - "137 offers",
"237 mails" - and you want to know which table holds them and which adapter put
them there.  It reads the database the app would read (``DREAMJOB_DB_PATH``,
honouring ``.env``) or the one given with ``--db``, counts the tables that
matter, and breaks the two that carry provenance down by source.

It opens the file with SQLite's ``mode=ro``, so it cannot write even by
mistake, and it prints no message bodies, no full addresses (the local part is
masked) and no credentials - only counts, domains and masked addresses, so the
output is safe to paste.

    python scripts/where_is_my_data.py
    python scripts/where_is_my_data.py --db /path/to/dreamjob.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

#: Tables worth counting, with what a row in each one actually means.  Keeping
#: the explanation next to the table is the whole point: a bare "vacancy: 137"
#: is what sends someone looking for mail that was never there.
TABLES: dict[str, str] = {
    "vacancy": "job adverts: boards, ATS, company sites, and job-alert e-mails (alert.*)",
    "opportunity": "scored rows for one campaign (a vacancy, or a speculative target)",
    "company": "employers discovered or enriched",
    "contact": "people found at those employers",
    "dispatch": "messages Dream Job sent",
    "incoming_reply": "messages read back from a connected mailbox (replies, bounces)",
    "mail_account": "connected mailboxes",
    "raw_document": "pages and files kept as fetched (FR-182 provenance)",
}


def mask_address(address: str | None) -> str:
    """``jane.doe@yahoo.fr`` -> ``j***@yahoo.fr``: the domain says which mailbox."""
    if not address:
        return "(none)"
    local, sep, domain = str(address).partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"


def default_db_path() -> Path:
    """The path the app itself would open, read from its settings."""
    # Run from a clone without installing the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from dreamjob.config import get_settings  # noqa: PLC0415

    return Path(get_settings().abs_db_path)


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot write: SQLite refuses any statement that would."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _print_block(title: str, rows: list[dict], key: str, empty: str) -> None:
    print(f"\n{title}")
    if not rows:
        print(f"  {empty}")
        return
    width = max(len(str(r[key] or "(unset)")) for r in rows)
    for row in rows:
        print(f"  {str(row[key] or '(unset)'):<{width}}  {row['n']:>6}")


def report(conn: sqlite3.Connection) -> None:
    """Print the counts and breakdowns for an open database."""
    print("\n--- row counts ------------------------------------------------")
    for table, meaning in TABLES.items():
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
            print(f"  {table:<16} {n:>7}   {meaning}")
        except sqlite3.Error as exc:  # an older schema may lack a table
            print(f"  {table:<16} {'n/a':>7}   ({exc})")

    # Where the adverts came from.  source_adapter is the honest answer to
    # "did this arrive by mail?": job-alert e-mails appear as alert.<source>.
    _print_block(
        "--- vacancies by source adapter -------------------------------",
        _rows(conn, "SELECT source_adapter, COUNT(*) AS n FROM vacancy "
                    "GROUP BY source_adapter ORDER BY n DESC"),
        "source_adapter",
        "no vacancies collected",
    )
    _print_block(
        "--- vacancies by access method (http / browser / bulk) --------",
        _rows(conn, "SELECT access_method, COUNT(*) AS n FROM vacancy "
                    "GROUP BY access_method ORDER BY n DESC"),
        "access_method",
        "no vacancies collected",
    )

    # What the reply poller has read, by sender domain only.
    _print_block(
        "--- replies read from a mailbox, by sender domain -------------",
        _rows(conn, "SELECT lower(substr(from_address, instr(from_address, '@') + 1)) AS domain, "
                    "COUNT(*) AS n FROM incoming_reply WHERE from_address IS NOT NULL "
                    "GROUP BY domain ORDER BY n DESC LIMIT 25"),
        "domain",
        "nothing read from a mailbox yet",
    )
    # A reply with no dispatch_id matched nothing Dream Job sent: recorded and
    # classified by the reply poller, and never a source of opportunities.
    # (Job-alert e-mails become vacancies through mail.job_alerts instead.)
    unmatched = conn.execute(
        "SELECT COUNT(*) FROM incoming_reply WHERE dispatch_id IS NULL"
    ).fetchone()[0]
    if unmatched:
        print(f"\n  of which {unmatched} matched no message Dream Job sent "
              "(recorded and classified; never turned into an opportunity)")

    print("\n--- connected mailboxes ---------------------------------------")
    accounts = _rows(
        conn,
        "SELECT a.backend, a.address, a.is_active, "
        "(a.credentials_enc IS NOT NULL) AS has_credential, "
        "p.folder, p.last_polled_at, p.messages_seen, p.last_error "
        "FROM mail_account a LEFT JOIN mail_poll_state p ON p.mail_account_id = a.id "
        "ORDER BY a.backend",
    )
    if not accounts:
        print("  none connected")
    for a in accounts:
        print(f"  {a['backend']} - {mask_address(a['address'])}")
        print(f"      active={bool(a['is_active'])} credential={bool(a['has_credential'])} "
              f"folder={a['folder'] or '-'} read={a['messages_seen'] or 0} "
              f"last_poll={a['last_polled_at'] or 'never'}")
        if a["last_error"]:
            print(f"      last error: {str(a['last_error'])[:120]}")

    print("\nNote: a mailbox connected for sending (gmail_oauth, imap_basic) is read for "
          "replies and bounces only (FR-326).\nJob-alert e-mails are read separately by "
          "mail.job_alerts and show up above as alert.* vacancies.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="override the database path")
    args = parser.parse_args(argv)

    db_path = args.db or default_db_path()
    print(f"database: {db_path}")
    if not db_path.exists():
        print("\nThat file does not exist, so nothing has been collected into it yet.")
        print("Check DREAMJOB_DB_PATH in your .env, or run the app once to create it.")
        return 1
    print(f"size:     {db_path.stat().st_size / 1_048_576:.1f} MB")
    conn = open_read_only(db_path)
    try:
        report(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
