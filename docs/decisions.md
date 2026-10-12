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

## 2026-10-11 — scores change meaning: area search, and a stricter dream-job score

Ported from stepvda/dreamjob (d01c0f1 and 318e83b, Stephane van der Aa), with
his macOS service files left out.

**What a score now means.** `pipeline/scoring.py` was reworked upstream: a
skill estimate shrinks towards a prior when the evidence is thin, a part of a
posting that cannot be assessed counts as neutral instead of as a match, a job
family needs two matches outside the title, and filler culture phrases are
ignored. `intelligence/gap_analysis.py` reads its weights from the scorer.
Fewer postings reach the top band on wording alone. **Scores computed before
this change are not comparable with scores after it** - re-score before
comparing two lists.

**Area search.** Opportunities can be filtered by distance (`near_lat`,
`near_lon`, `radius_km`, `include_remote`), with place autocomplete. Every
opportunity is now placed on the map by a `geolocation` scheduler task
(every 2 minutes, through Nominatim at one request per second, as the existing
`geocode.py` already does; migration 158), which replaces the manual locate
button. This is a new background process that sends requests to a third party.

## 2026-10-12 — the semantic index may enter the dream fit, off by default

**What.** `DREAMJOB_SEMANTIC_SCORING` (default `false`). When on, and an
embeddings model is configured, the mechanical dream-fit sub-score becomes
`(0.5 x title + 0.5 x criteria + 0.25 x similarity) / 1.25` instead of
`(0.5 x title + 0.5 x criteria) / 1.0`, where similarity is the cosine between
the vacancy's stored vector and the embedded dream job, clamped to 0..1. A
vacancy with no stored vector keeps the two-part formula.

**Why off.** It changes what a score means, and a raw cosine is not on the
same scale across embeddings models. With the setting off, `_weighted` drops
the missing term, the arithmetic is the two-part one exactly, and the embedder
is never called - so no existing score changes.

**Why a fifth.** The term measures topical closeness of whole texts, which the
title and criteria terms already approximate; it is meant to break ties among
adverts the mechanical terms rate alike, not to outvote them.

**What would reverse it.** A per-model calibration of the cosine, after which
the term could be on by default - with a re-score, since scores before and
after would not be comparable.
