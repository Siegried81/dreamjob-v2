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

## 2026-10-12 — a boolean app setting switched off now reads as off

**The bug.** `knowledge.set_setting` wrote every non-container value with
`str()`, so `False` was stored as the text `False`. That is not JSON, so
`get_setting` returned the string `"False"`, and every reader that wraps it in
`bool()` saw `True`. An administrator who switched the contact search provider
off (`PUT /api/contacts/search-provider` with `enabled: false`) left it on;
the contacts lookup service and continuous collection (`pipeline/continuous.py`)
read their switches the same way. Proved by
`tests/unit/test_search_provider_enrichment.py`, which fails on the old code.

**The fix.** Booleans are now written as JSON (`true`/`false`), and the two
legacy texts `True`/`False` already stored in a database are read back as
booleans, so existing installations are corrected without a migration.

**What changes for a reader.** A switch that was turned off and kept running
now stops. Nothing that was on changes.

## 2026-10-12 — profile enrichment uses the configured search provider

`pipeline/enrichment.web_search` asks the search API configured for FR-303
(Brave, Bing or Google CSE) when it is switched on, and DuckDuckGo otherwise
or when the API fails for a query. Enrichment queries contain the job seeker's
name; they already left the machine for DuckDuckGo under the FR-126
enrichment consent, and now go to the provider the administrator chose
instead. The request shape per provider lives once, in
`email_patterns.provider_query`, shared with the contacts search.

## 2026-10-12 — research passes skip agencies and job boards

**What changed.** `company_profile.rerun`, `financial.rerun`,
`financial.financial_worker` and `speculative.candidate_companies` now drop
every company whose effective employer role is `agency` or `board`
(`employer_product.intermediary_company_ids`, the seeker's own correction
applied as on the badge). Unresearched and `unverified` companies still pass.

**What a number means.** In the re-run reports, `companies` now counts the
companies actually worked on, and the new `skipped_intermediaries` counts the
ones left out; before, an agency was counted and profiled like any employer.
No score changes: scoring already refused an intermediary's company
dimensions, and stored profiles and filings are left as they are.

**Why.** `docs/Employer_Kind_Results.md` §7 item 5: the gates existed in the
repository and nothing called them, so an agency could still be crawled,
pitched a speculative opening and have its accounts collected.

**What would reverse it.** Dropping the call in one of the four passes; the
helper fails open (an unreadable verdict table skips nothing).

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

## 2026-10-12 — French companies are read from recherche-entreprises.api.gouv.fr

**What changed.** `registry.recherche_entreprises` is the registry for `FR` in
`pipeline/financial.REGISTRIES_BY_JURISDICTION`. Until now a French company
fell through to OpenCorporates and the FR-245 estimated path; on its next
financial pass it gets the register's identity (SIREN, NAF code, headcount
band) and, where published, revenue and net result written as registry
figures, not estimates. Stored rows do not change until that pass runs.

**What a number means.** The ability-to-pay and trajectory figures of a French
company that has published accounts now rest on two registry fields (revenue,
net result) instead of secondary signals. Components that need a balance sheet
or personnel costs stay unknown - the API publishes neither. `size_fte` is the
lower bound of INSEE's headcount band, not a measured headcount.

**Why this source.** It is the State's own free, keyless API (DINUM) over INSEE
Sirene and the INPI RNE, under the Licence Ouverte. Germany was looked at and
skipped: the Handelsregister has no official API and its terms cap retrieval at
60 an hour.

**What would reverse it.** Removing the `FR` entry sends French companies back
to the estimated path; the adapter itself stays harmless.

## 2026-10-12 — one vacancy row per dedup key

**What changed.** `vacancy.dedup_key` was under a plain index; migration 160
makes it unique (`WHERE dedup_key IS NOT NULL`) after folding the duplicates
already stored. `KnowledgeBaseWriter._write_vacancy` turns the resulting
IntegrityError into a merge into the row that won, and a fuzzy merge that would
copy a key another row holds keeps the matched row's own key instead.

**Why.** The installed database held 15,697 vacancies of which 242 keys were
stored twice, every pair from the same adapter (`raw_document_id` shared
in 155 pairs), all collected on 2026-10-04 between 14:20 and 15:07 UTC). The
commit history starts on 2026-10-06, so how that run wrote them is not readable
here. Two paths in the current writer can still produce it, and both are now
reproduced by tests that fail without the change: a writer whose key lookup ran
before another writer committed, and a fuzzy merge copying the incoming key onto
a row while a second row already holds that key.

**Which row survives.** The newest of each key (`collected_at`, then `id`). It is
the row `vacancy_by_dedup_key` already returned, so every merge since the pair
appeared went into it. Of the 242 older rows on the installed database, 228 had
no opportunity, 4 had theirs only on the newer row, and 10 had an opportunity on
both rows in the same campaign. `idx_opp_campaign_vacancy` allows one per
campaign, so those 10 cannot be moved: an untouched copy (status `new`, not
selected, pinned or ranked, nothing built on it) is deleted, and one the job
seeker acted on is kept and only loses its vacancy link. All 10 on the installed
database were untouched. Provenance follows the surviving row; embeddings of the
deleted rows are dropped.

**What it means for the numbers.** Vacancy counts fall by the number of folded
rows (242 on the installed database) and the opportunity list of the campaigns
concerned loses its duplicates. A posting stored under two different source URLs
with the same title, employer, place and week keeps one URL: 10 of the 242 pairs
were such, and the writer already treated each as one posting.

**What would reverse it.** Dropping the unique index brings back a plain one; the
rows deleted by migration 160 are only in a backup taken before it ran.
