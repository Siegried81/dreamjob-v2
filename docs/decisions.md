# Decisions

Choices that the code alone doesn't explain: what was decided, why, and what
would reverse it. Git carries authorship, this file carries the reasoning.
Oldest first.

## 2026-10-06: `board.jobat` needs an IR-101 acknowledgement

`requires_ack` is now true and the catalogue row matches, so the adapter stays
off until an administrator acknowledges it. It had collected for real: 23 cached
pages, all HTTP 200, and a later 403 run, while the data-gathering rules say not
to touch Jobat. Acknowledgement rather than deletion, because the legal reading
isn't settled and `requires_ack` is the gate built for this case. The job-alert
mail route (`alert.jobat`) reads the seeker's own mailbox and is unaffected.

## 2026-10-10: the quickstart installs before it generates keys

`requirements.txt` doesn't install the package under `backend/`, so every
`python -m dreamjob...` command needs `PYTHONPATH=backend` and the pip install
has to come before `--generate-key`. The README, the architecture doc,
`scripts/dev.sh` and the `bootstrap.py` warning say so. Making the package
installable (`-e .`) would remove the prefix.

## 2026-10-11: `tzdata` is a declared dependency

Windows has no system time-zone database, so a venv built from the requirements
alone failed 50 tests on `ZoneInfo("Europe/Brussels")`. Declared explicitly.

## 2026-10-11: scores changed meaning, and area search was added

Ported from the upstream project. A skill estimate shrinks towards a prior when
evidence is thin, a part of a posting that can't be assessed counts as neutral,
a job family needs two matches outside the title, and filler culture phrases are
ignored. Scores computed before this aren't comparable with scores after it:
re-score before comparing two lists. Opportunities can be filtered by distance
(`near_lat`, `near_lon`, `radius_km`, `include_remote`), and a `geolocation`
scheduler task places each one on the map through Nominatim at one request per
second (migration 158). That is a background process calling a third party.

## 2026-10-12: a boolean app setting switched off reads as off

`set_setting` wrote `False` as the text `False`, which `bool()` read as true, so
an administrator who switched the contact search provider off left it on.
Booleans are stored as JSON now, and the two legacy texts are read back as
booleans, so no migration is needed. A switch that was off now stays off.

## 2026-10-12: profile enrichment uses the configured search provider

`pipeline/enrichment.web_search` asks the provider set for FR-303 (Brave, Bing,
Google CSE) when it is on, and DuckDuckGo otherwise or on failure. The queries
carry the seeker's name; they already left the machine under the FR-126
consent, and now go to the provider the administrator chose. The request shape
lives once in `email_patterns.provider_query`.

## 2026-10-12: research passes skip agencies and job boards

`company_profile.rerun`, `financial.rerun`, `financial.financial_worker` and
`speculative.candidate_companies` drop companies whose effective employer role
is `agency` or `board`, with the seeker's own correction applied. Reports now
count companies actually worked on, plus `skipped_intermediaries`. Scores don't
change, since scoring already refused an intermediary's company dimensions.
The helper fails open: an unreadable verdict table skips nothing.

## 2026-10-12: the semantic index may enter the dream fit, off by default

With `DREAMJOB_SEMANTIC_SCORING=true` and an embeddings model, the mechanical
dream fit is `(0.5 title + 0.5 criteria + 0.25 similarity) / 1.25`; a vacancy
without a stored vector keeps the two-part formula. Off by default because a raw
cosine isn't on the same scale across models, and turning it on needs a re-score.
A fifth of the weight, because it should break ties, not outvote the title.

## 2026-10-12: French companies are read from recherche-entreprises.api.gouv.fr

`registry.recherche_entreprises` is the `FR` registry. A French company gets its
SIREN, NAF code and a headcount band, and where accounts are published, revenue
and net result as registry figures instead of estimates. No balance sheet or
personnel costs exist in that API, so those stay unknown, and `size_fte` is the
lower bound of INSEE's band. Germany has no adapter: the Handelsregister has no
official API and caps retrieval at 60 an hour.

## 2026-10-12: one vacancy row per dedup key

Migration 160 makes `vacancy.dedup_key` unique after folding duplicates (242
keys were stored twice on the installed database). The writer turns the
resulting IntegrityError into a merge into the winning row, and a fuzzy merge
never copies a key another row holds. The newest row survives. Vacancy counts
fall by the rows folded; the deleted rows exist only in a pre-migration backup.

## 2026-11-04: the semantic index can live in Qdrant, off by default

`DREAMJOB_SEMANTIC_BACKEND=qdrant` stores vectors in a Qdrant server instead of
the `embedding` table; `qdrant-client` is optional. The similarity is the same
cosine either way. The content-hash skip is keyed by model, so switching model
re-embeds everything, and one collection holds one dimension: drop it first when
the size changes.

## 2026-11-08: digest counts are totals

The digest read each section's count as `len()` of a list fetched with
`LIMIT 24`, so 41 new opportunities showed as 24. Counts now come from one
`COUNT(*)` per section (`pipeline_cards.digest_counts`). Stored digests keep
their old counts.

## 2026-11-08: the knowledge-base half of the synthesis pool is not capped

`vacancies_for_companies` kept the newest 5,000 rows, and a pool of 5,200 came
back as 5,000. It now returns all of them, binding company ids 500 at a time
(SQLite stops at 32,766 variables). The campaign's own `max_opportunities` cap
still applies and is still reported.

## 2026-11-08: learned scoring weights stay inside their band

`learn_weights` clamped to 0.3x..2.5x of the default and then normalised, which
pushed `profile_fit` to 2.86x. It now uses `normalise_within_band`, which pins a
weight at its edge and rescales only the others, so it stops at 2.5x (0.55).

## 2026-11-18: stale vacancies that no opportunity uses are deleted

A daily `vacancy_purge` task deletes vacancies that no opportunity points at and
that nobody has seen for the staleness window, never under 7 days, with their
provenance and embeddings. `scripts/purge_unlinked_vacancies.py` does the same by
hand (counts by default, backs up before `--apply`);
`DREAMJOB_VACANCY_PURGE_SECONDS=0` turns the task off. A vacancy behind an
opportunity is kept at any age. On the development database, 14,626 of 17,323
vacancies were unlinked.

## 2026-11-18: the CR-410 consent names the model API in use

The consent text names the host of `DEEPSEEK_BASE_URL`, plus the fallback host
when a fallback key is set, instead of always saying "the DeepSeek API". The text
shown is the text stored with the decision. Changing the endpoint later doesn't
reset an existing consent.

## 2026-11-20: deepseek-flash for both roles, thinking off for cheap calls

DeepSeek retired `deepseek-chat` and `deepseek-reasoner`, so `deepseek-flash`
takes both roles, main and fallback. The role travels in the request:
`llm.client.thinking_switch` turns thinking off on cheap calls and on for strong
ones, only for `api.deepseek.com`. On `classify.employer_kind` (7 labelled sites,
2 repeats) thinking gave the same 14/14 verdicts for 2.4x the output tokens and
cut 4 of 14 calls at the 900-token cap. The response-cache key includes the role,
so a strong retry isn't served the cached cheap answer, and the fallback picks
its model from the role. Known limit: pinning a task to "Cheap" in the admin
screen changes nothing while DeepSeek is the main endpoint.

## 2026-11-20: the FR-425 outcome nudge moves the weights the scorer reads

The nudge was filed under column names (`score_plausibility`) while the scorer
reads `plausibility`, started from 1.0 instead of the stored weight, and ignored
the band, so applying the learning changed no ranking. It now uses the component
names, starts from the weights in use and is saved through
`normalise_within_band`. Plausibility or reachability moves at most 15% per
application and can't leave 0.3x to 2.5x of its default.

## 2026-11-20: unit tests can't reach the internet

A campaign run ends in passes that look up every employer. Tests that didn't stub
them waited on the real internet at the per-domain pace: 59 s for one test, 63 to
71 minutes for the suite. `tests/unit/conftest.py` refuses name resolution and
socket calls to every host but loopback, so a missed stub is a fast failed lookup.
The suite runs in about 9 minutes.

## 2026-11-20: helpers that were written and never called are wired

A finished collection fills the country of country-less companies from where most
of their postings are, so the planner drops more out-of-scope companies. The
Apply Browser selection status follows the package (generating, ready, failed,
approved, sent). A finished campaign restamps its browser-collected contacts from
its real end plus the grace period (NFR-303). A new campaign takes the default
persona, and another seeker's persona is refused. Expired OAuth states are purged
when a new one is created. Digests are one per seeker per calendar day, not per
second.

## 2026-11-20: Google Calendar can be connected

`/api/pipeline/calendar/authorize` stores a single-use state and
`/api/pipeline/calendar/callback` exchanges the code. The redirect is
`GOOGLE_CALENDAR_REDIRECT_URI`, never a value sent by the client. There is no
screen for it yet. Yahoo isn't built: it would need CalDAV with an app password.

## 2026-11-20: no built-in sender, owner address or crawler contact

The repo is public, so no personal address is built in. The sender is
`DREAMJOB_MAIL_FROM`, the administrator `DREAMJOB_OWNER_EMAIL` and
`DREAMJOB_OWNER_NAME`, and the crawler appends `(contact: ...)` to its
`User-Agent` only when `DREAMJOB_CRAWLER_CONTACT` is set. With no sender the
Resend backend reports itself unavailable. Resend can't send as an `@gmail.com`
address, so sending as Gmail goes through the Gmail connection.

## 2026-11-20: third-party XML goes through defusedxml

Personio, Teamtailor, the Actiris sitemap and RSS are parsed by
`pipeline/safe_xml.py`, which refuses entity declarations (billion laughs,
external entities) and reports them as the `ParseError` the adapters already
handle.

## 2026-11-22: the employer-kind helpers are wired

`work_queue` orders each pass suspicion first, then vacancy count: the queue is
still chosen by vacancy count, then `resolve_many` walks it as the detector
ranks it, so a pass that stops early has covered the suspected agencies first.
`registry_signals` feeds R1 and D6 into `Signals.with_signals` after the registry
rung, and the recomputed band replaces the stored rung-0b snapshot. An ambiguous
register name is stored as `cannot_tell(registry_ambiguous)` with the candidate
rows (`ambiguity_verdict`). `read_signals` puts the stored snapshot in the
`/employers/{id}/kind` payload as `free_signals`. Verdicts don't change, since
only a rung's own evidence decides.

Measured on the 43 labelled employers with the register's answer simulated from
each label source (no live register run was possible):

| Band | Before | After |
|---|---|---|
| probable and above | P 1.00, R 0.55 | P 1.00, R 0.65 |
| possible and above | P 1.00, R 0.85 | P 1.00, R 0.90 |
| direct employers newly tagged as agency | none | none |

Seven employers change band, none towards agency among the direct ones. Detail in
`docs/Employer_Kind_Results.md` section 9.

## 2026-11-23: a privacy-routed LLM task never reaches the cloud

A task listed in `DREAMJOB_LOCAL_LLM_TASKS` (profile, CV, motivation, NFR-306)
went to the local model, but when that call failed the client tried the paid
fallback endpoint with the same prompt, and with no local URL set the task went
to the cloud without a word. Both defeated the listing. Now a privacy-routed
call never gets the fallback endpoint, and a listed task with no local URL raises
an `LLMError` that says so. Tasks that aren't listed behave as before. This
matches the DPIA, which presents the local model as the way to remove the
transfer entirely for these steps.


## 2026-11-23: a reasoning model that runs out of budget gets a second try

On Groq's gpt-oss models the reasoning counts against `max_tokens`. A cheap call
such as `classify.employer_kind` (900 tokens) could use all of it and come back as
`400 json_validate_failed` with an empty `failed_generation`; the client treated
a 400 as final and, with no fallback key, the employer got no verdict. Cheap calls
to Groq gpt-oss now send `reasoning_effort: "low"` (strong calls keep the
default), and a `json_validate_failed` 400 is retried once on the same endpoint
with twice the `max_tokens` (up to 8,192). If that fails too, the call raises as
before and the website rung retries once on the strong model. The "LLM endpoint
failed" warning and the `LLMError` now carry the provider's status, error code and
the first 300 characters of the response body, never the request headers or the key.

## 2026-11-25: photos are decoded only as known raster formats

`identity_match.perceptual_hash` opens bytes with `formats=PHOTO_FORMATS`: JPEG,
PNG, WEBP, GIF and AVIF (what a web page serves), plus BMP, TIFF and JPEG 2000
(what a CV embeds: BMP in a .docx, the other two from a PDF via pypdf). Pillow
reads EPS by calling Ghostscript when one is installed, and Upload2Shell
(CVE-2026-125396) makes a crafted file a code-execution path; candidate photos
come from arbitrary web pages. No machine or image here has Ghostscript today.
A photo in any other format gives no photo signal, as an undecodable one already
did. Revisit if a real portrait turns up in a format outside the list.
