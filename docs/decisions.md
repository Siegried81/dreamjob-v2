# Decisions

A dated record of choices that are not readable from the code alone - what was
decided, why, and what would reverse it. Git carries authorship; this file
carries the reasoning.

## 2026-10-06 — `board.jobat` now requires an IR-101 acknowledgement

**What changed.** `adapters/jobboards/jobat.py` had `requires_ack = False` with
`tos_status = RESTRICTED`. It is now `requires_ack = True`, and the
`source_catalogue` row was updated to match, so `is_enabled()` refuses the
adapter until an administrator acknowledges it.

**Why.** The adapter had actually collected. `http_cache` holds 23 rows for
`www.jobat.be/nl/jobs?...`, every one HTTP **200**, between 2026-10-04 14:14:04
and 15:03:01, and `adapter_decline` records `board.jobat / http_403` with
request_count 7 / refused_count 6 from a later run. Meanwhile
`docs/Data_Gathering_Plan.md` rule 1 says "Do not touch Indeed, LinkedIn,
StepStone or Jobat", and gives as its reason that circumventing Jobat's 403 "is
exactly the behaviour the product promises not to engage in".

The adapter's own docstring argues that the 403 makes staying enabled harmless
and lets the operator see the failures. The 23 successes refute the premise: the
403 is intermittent, so "enabled" meant "collects whenever Cloudflare lets it".

**Why acknowledgement rather than deletion.** The adapter is working code and the
legal position is arguable - a low-rate read of public advert pages, storing only
what the advert states. What was not arguable is that it ran with no recorded
human decision while a written rule forbade it. `requires_ack` is the gate this
repo already built for exactly that situation (`adapters/base.py:175-186`), so
using it is cheaper and more honest than a second mechanism.

**What this does not change.** The job-alert e-mail route
(`mail/job_alerts.py`, source `alert.jobat`) is untouched and unaffected: it
reads the seeker's own mailbox, makes no HTTP call, and stores no Jobat URL.
That route is the lawful alternative the rule exists to push towards.

**What would reverse it.** An administrator acknowledging the source, which is
the point - the decision becomes visible and attributable. Deleting the adapter
instead would also be defensible; it was not chosen because it discards work
whose legal reading has not actually been settled.

## 2026-10-11 — a fresh Windows clone failed 50 tests for want of a time-zone database

`requirements.txt` never named `tzdata`. On Linux `zoneinfo` reads the system
database; Windows has none, so `ZoneInfo("Europe/Brussels")` and even
`ZoneInfo("UTC")` raised `ZoneInfoNotFoundError` in every test touching a send
window, a relative date or the apply browser: 50 failures, 2,120 passed, on a
venv built from the requirements alone. The older clone passed because another
package had pulled `tzdata` in transitively. Declared explicitly; the four
modules go from 50 failures to 128 passed. Same fix as the fixtures attribute
committed earlier today: the repository must be true on the machine it is
cloned to, not only on the one it was written on.

## 2026-10-10 — The quickstart installs before it generates keys

**What.** README, `docs/Technical_Architecture.md`, `scripts/dev.sh` and the
`bootstrap.py` warning now put `python3 -m venv` and `pip install` before
`dreamjob.security.crypto --generate-key`, and prefix every `python -m
dreamjob...` line with `PYTHONPATH=backend`.

**Why.** The package lives under `backend/` and `requirements.txt` does not
install it, so the documented first command failed with `ModuleNotFoundError`
on a fresh clone: the generator imports the package's own dependencies, which
were not installed yet, and the package itself, which was not on the path.
`scripts/dev.sh` already set `PYTHONPATH=backend`; the commands a reader types
by hand did not.

**What would reverse it.** Making the package installable from
`requirements.txt` (`-e .`), after which the prefix becomes unnecessary and the
order stops mattering.
