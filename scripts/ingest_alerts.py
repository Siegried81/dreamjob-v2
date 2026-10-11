"""One job-alert pass you can read the verdict of: back up, ingest, say what is safe to delete.

    PYTHONPATH=backend python scripts/ingest_alerts.py

Why a script when the scheduler already runs ``job_alerts`` every fifteen
minutes: the scheduler reports totals in a log, and the question a mailbox
owner has is "may I delete these mails now?". That answer is the cursor: a
job-alert mail from a recognised sender, dated before the cursor of a pass that
ended with no error, has had its postings read into the database. This script
prints that one line, after doing two things the scheduler does not:

- it writes every alert mail in the pass window to ``data/alerts_backup/<uid>.eml``
  before anything is parsed, because the database keeps the postings and not
  the mail, so a deleted mail is otherwise gone;
- it lifts ``MAX_MESSAGES`` for this run when more mails are waiting than one
  pass would take: ``ingest_account`` keeps the newest N and moves the cursor
  past the rest, so a backlog larger than N would be skipped for good.

The mailbox is opened read-only, as ``ImapAlertMailbox`` always is. Nothing
here prints a setting's value.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from dreamjob.config import get_settings
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.mail import job_alerts

BACKUP_DIR = Path("data/alerts_backup")
TOTAL_KEYS = ("mailboxes", "errors", "alerts", "alerts_without_postings", "alerts_vanished",
              "postings", "new_vacancies", "opportunities_added", "notifications")


def window_cap(waiting: int, default: int) -> int:
    """The per-pass cap to use: the default, or the whole window when it is larger.

    ``ingest_account`` reads the newest ``cap`` messages and then moves the
    cursor to the pass start, so with ``waiting > default`` the oldest
    ``waiting - default`` mails would never be read. Lifting the cap for the run
    is the only way to keep the cursor honest.
    """
    return max(default, waiting)


class _ErrorCounter(logging.Handler):
    """Counts what the pass only logs.

    ``ingest_account`` catches a failing sub-step, logs it with
    ``log.exception`` and carries on, and ``totals["errors"]`` stays at 0: a
    pass can store every posting and still have failed to create the
    opportunities (measured: ``CryptoUnavailable`` when ``DREAMJOB_MASTER_KEY``
    was missing, with ``errors: 0``). A verdict that does not see the log says
    "safe" over a broken pass, so the log is read too.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text += f" ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
        self.messages.append(text)


def verdict_line(totals: dict, cursor_after: str | None, logged_errors: int = 0) -> str:
    """The sentence the mailbox owner acts on."""
    if totals.get("errors") or logged_errors:
        return (f"NOT safe to delete: {totals.get('errors', 0)} counted error(s) and "
                f"{logged_errors} logged error(s) in this pass; fix them and re-run before "
                "deleting anything.")
    if not cursor_after:
        return "NOT safe to delete: no cursor was stored, so no mail is known to have been read."
    return (f"Safe to delete alert mails from recognised senders dated before {cursor_after}: "
            f"{totals.get('alerts', 0)} alerts read, {totals.get('postings', 0)} postings, "
            f"{totals.get('new_vacancies', 0)} new vacancies, 0 errors.")


def alert_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return dict(conn.execute(
            "SELECT source_adapter, COUNT(*) FROM vacancy "
            "WHERE source_adapter LIKE 'alert.%' GROUP BY 1"))
    finally:
        conn.close()


def main() -> int:
    settings = get_settings()
    user, password = settings.alerts_imap_user.strip(), settings.alerts_imap_password
    if not user or not password:
        print("DREAMJOB_ALERTS_IMAP_USER / DREAMJOB_ALERTS_IMAP_PASSWORD are not set; nothing done.")
        return 2
    db = settings.abs_db_path
    before = alert_counts(db)
    cursor_key = f"{job_alerts.SETTING_CURSOR_PREFIX}imap:{user}"
    cursor = admin_repo.get_setting(cursor_key)
    print(f"cursor before: {cursor or 'none (first window: 14 days)'}")

    # Backup first, over the same window the pass will read.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    written = present = 0
    with job_alerts.ImapAlertMailbox(settings.alerts_imap_host, user, password) as mailbox:
        uids = mailbox.alert_ids(cursor, 100_000)
        for uid in uids:
            target = BACKUP_DIR / f"{uid.decode()}.eml"
            if target.exists():
                present += 1
                continue
            target.write_bytes(mailbox.fetch_raw(uid))
            written += 1
    print(f"window: {len(uids)} alert mail(s); backup: {written} written, {present} already in {BACKUP_DIR}")

    cap = window_cap(len(uids), job_alerts.MAX_MESSAGES)
    if cap != job_alerts.MAX_MESSAGES:
        print(f"cap lifted for this run: {job_alerts.MAX_MESSAGES} -> {cap}, so no mail is skipped")
        job_alerts.MAX_MESSAGES = cap

    totals = {k: 0 for k in TOTAL_KEYS}
    counter = _ErrorCounter()
    logging.getLogger("dreamjob").addHandler(counter)
    try:
        job_alerts._ingest_imap(totals)
    finally:
        logging.getLogger("dreamjob").removeHandler(counter)
    for message in counter.messages:
        print("logged error:", message[:160])
    after = alert_counts(db)
    cursor_after = admin_repo.get_setting(cursor_key)
    shown = ("alerts", "alerts_without_postings", "postings", "new_vacancies", "errors")
    print("pass:", {k: totals[k] for k in shown})
    for key in sorted(set(before) | set(after)):
        print(f"  {key:16s} {before.get(key, 0):5d} -> {after.get(key, 0):5d}")
    print(verdict_line(totals, cursor_after, len(counter.messages)))
    return 1 if totals["errors"] or counter.messages else 0


if __name__ == "__main__":
    sys.exit(main())
