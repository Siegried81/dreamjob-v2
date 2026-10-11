"""The two decisions ``scripts/ingest_alerts.py`` makes, tested without a mailbox.

The script's I/O is the app's own ``ImapAlertMailbox`` and ``_ingest_imap``,
both covered elsewhere; what is new here is when the per-pass cap is lifted
and what sentence the owner reads, and both are pure functions.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ingest_alerts", Path(__file__).resolve().parents[2] / "scripts" / "ingest_alerts.py")
ingest_alerts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ingest_alerts)


def test_the_cap_is_lifted_only_when_more_mails_wait_than_a_pass_reads():
    # ``ingest_account`` keeps the newest N and moves the cursor past the rest:
    # a backlog of 77 against a cap of 50 would lose 27 mails for good.
    assert ingest_alerts.window_cap(77, 50) == 77
    assert ingest_alerts.window_cap(12, 50) == 50
    assert ingest_alerts.window_cap(0, 50) == 50


def test_the_verdict_names_the_cursor_and_refuses_on_any_error():
    ok = {"errors": 0, "alerts": 72, "postings": 514, "new_vacancies": 147}
    line = ingest_alerts.verdict_line(ok, "2026-10-11T07:44:27+00:00")
    assert line.startswith("Safe to delete")
    assert "2026-10-11T07:44:27+00:00" in line
    assert "147 new vacancies, 0 errors" in line

    assert ingest_alerts.verdict_line({**ok, "errors": 1}, "2026-10-11T07:44:27+00:00").startswith("NOT safe")
    assert ingest_alerts.verdict_line(ok, None).startswith("NOT safe")


def test_an_error_the_app_only_logged_still_refuses_the_verdict():
    """Measured: a missing DREAMJOB_MASTER_KEY raised CryptoUnavailable inside the
    pass, the app logged it and carried on, and totals["errors"] stayed 0."""
    import logging
    ok = {"errors": 0, "alerts": 15, "postings": 71, "new_vacancies": 1}
    counter = ingest_alerts._ErrorCounter()
    log = logging.getLogger("dreamjob.test_ingest_alerts")
    log.addHandler(counter)
    try:
        try:
            raise RuntimeError("DREAMJOB_MASTER_KEY is not set")
        except RuntimeError:
            log.exception("Job-alert pass failed for the IMAP mailbox")
        log.warning("a warning is not an error")
    finally:
        log.removeHandler(counter)
    assert len(counter.messages) == 1
    assert "RuntimeError" in counter.messages[0]
    assert ingest_alerts.verdict_line(ok, "2026-10-11T09:23:30+00:00", len(counter.messages)).startswith("NOT safe")
    assert ingest_alerts.verdict_line(ok, "2026-10-11T09:23:30+00:00", 0).startswith("Safe")

