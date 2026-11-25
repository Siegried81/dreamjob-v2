# Dream Job: Technical Architecture

| | |
|---|---|
| Document | Technical Architecture (TA) |
| Version | 1.1 |
| Date | 11 September 2026 |
| Status | Describes the implemented system |
| Specifies | Dream Job SRS v0.3, technology constraints CR-407 to CR-410 |
| Companion | [Functional Design](Functional_Design.md) · [DPIA](DPIA.md) |

---

## 1. Constraints and what follows from them

The SRS fixes the stack (§2.5, CR-407 to CR-410). Everything below is a consequence
of taking those constraints seriously rather than working around them.

| Layer | Fixed choice | What it forces |
|---|---|---|
| Frontend | React SPA | Route-level code splitting; the API is the only contract |
| Backend | Python | FastAPI, asyncio, in-process job runner |
| Database | SQLite | WAL mode, **one writer**, SQL confined to a repository layer |
| LLM | DeepSeek | OpenAI-compatible transport, so a local model substitutes without code change |
| Browser | CDP via Playwright | Attach-only; no credential handling anywhere in the codebase |

> **The most consequential constraint is SQLite.** One writer means the job
> runner is in-process and cooperative rather than a worker pool, and it means
> writes are serialised deliberately instead of contended for. That is a
> reasonable trade for a single-node product and it is the assumption to revisit
> first if multi-user concurrency ever becomes real (CR-408 anticipates exactly
> this).

---

## 2. System shape

```
┌──────────────────────────────────────────────────────────────────────┐
│  React SPA       24 screens · lazy-loaded · 107 KB initial (gzip)    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  /api  (session cookie, client-bound)
┌───────────────────────────────▼──────────────────────────────────────┐
│  FastAPI         23 routers · 364 paths                              │
│                  current_seeker is the isolation boundary            │
├──────────────────────────────────────────────────────────────────────┤
│  Pipeline        intake · enrichment · employer kind · planning ·    │
│                  collection · profiling · domains · financial ·      │
│                  opportunities · scoring · contacts · generation ·   │
│                  post-application                                    │
├───────────────┬──────────────┬───────────────┬──────────────┬────────┤
│  LLMClient    │ EgressClient │ SourceAdapter │  JobRunner   │ crypto │
│  budget       │ robots       │ 30 enabled    │  resumable   │ AES-   │
│  redaction    │ rate limit   │ plan/fetch/   │  pause/      │ GCM    │
│  injection    │ cache        │ parse/        │  cancel      │ per    │
│  fencing      │ raw capture  │ normalise     │  checkpoint  │ seeker │
│  audit        │              │               │              │        │
├───────────────┴──────────────┴───────────────┴──────────────┴────────┤
│  Repositories    the only place SQL is written                       │
├──────────────────────────────────────────────────────────────────────┤
│  SQLite (WAL)  101 tables · 158 indexes │  Filesystem: raw docs,    │
│  private ▸ seeker, per query            │  generated CVs and PDFs,   │
│  shared  ▸ knowledge base, no link back │  uploads, exports          │
└──────────────────────────────────────────────────────────────────────┘
        │              │              │              │
   DeepSeek API   Job boards /    Registries    Gmail OAuth /
   (or local)     ATS / sites     NBB, KBO…     Resend · M365 (read)
```

**Scale** (`backend/**/*.py`, `frontend/**/*.{js,jsx}`, `tests/**/*.py`).
247 Python modules / 129,071 lines · 147 JS modules / 45,057 lines · 153 test
files / 72,107 lines · 46 migrations · 24 versioned prompt templates · 36
declared source adapters, 30 of them enabled. The offline pass count is in
`README.md`. Lines are counted with `git ls-files 'backend/*.py' | xargs cat | wc -l`,
and the same with `frontend/*.js frontend/*.jsx` and `tests/*.py`.

---

## 3. The five chokepoints

The architecture rests on funnelling five concerns through exactly one place
each. That is what makes the corresponding requirements *enforceable* rather
than aspirational, each is a property of one module, not a convention 245
modules have to remember. Two more funnels were added as the Discover phase
grew: the employer-kind verdict and the company-domain decision.

| Chokepoint | Module | Enforces |
|---|---|---|
| Every SQL statement | `db/repositories/*`, `db/connection.py` | CR-408 portability, FR-344 isolation |
| Every outbound HTTP request | `egress/client.py` | FR-182 robots + rate limits, FR-183 raw capture, IR-102 |
| Every LLM call | `llm/client.py` | NFR-104 budget, NFR-205 injection defence, CR-410 redaction, FR-362 routing, FR-364 audit |
| Every external source | `adapters/base.py` | NFR-601 extensibility, IR-101 terms status |
| Every long-running task | `jobs/runner.py` | FR-185 control, NFR-401 resumability |
| Every employer-kind verdict | `pipeline/employer_resolver.py` | FR-341, NFR-402 no verdict without evidence |
| Every company-domain decision | `pipeline/company_domains.py` | DR-101 identity, NFR-402 provenance |

### 3.1 Persistence: `db/`

```python
read_tx()    # concurrent readers under WAL
write_tx()   # one global lock; SQLite serialises anyway, so queueing
             # in-process turns "database is locked" into an orderly wait
```

Repositories return plain dicts, never cursors. JSON columns encode and decode
at the boundary. Migrations are forward-only `NNN_name.sql` files with a
recorded checksum; editing an applied migration is a warning, not a silent
divergence.

**Schema shape.** 92 logical tables across 46 migrations (49 seeker-scoped, 40
shared, one migration ledger, and two FTS5 virtual tables; the nine FTS5 shadow
tables take `sqlite_master` to 101) plus a
`usable_contact` view that encodes the objection rule at the database:

- **Private** tables carry `job_seeker_id`: profile and versions, conflicts, skills,
  evidence, personas, enrichment findings, composite profile, dream job model,
  directives, campaigns, plan items, opportunities, application packages,
  dispatches, replies, pipeline cards, mock interviews, gap analyses, exports,
  watchlist entries, apply selections and the apply preference.
- **Shared**: the knowledge base, carrying **no** link back to a job seeker:
  companies, company domains and domain candidates, financial years and
  analyses, competitor links, hiring signals, employer-kind verdicts and
  resolution attempts, registry identity, review summaries, vacancies,
  compensation observations, board registry, company seed, embeddings, raw
  documents, events, geocoded places and source catalogue.
- **Restricted**: `contact`, which is shared *except* when collected through
  browser automation, where `shareable = 0`, `owning_campaign_id` is set and
  `retention_until` applies (NFR-303).

Full-text search over companies and vacancies uses FTS5, `vacancy_fts` is
external-content with write triggers, and `company_fts` is keyed on the
company row id, so a write no longer scans the corpus (NFR-103).

### 3.2 Egress: `egress/client.py`

One async client for all outbound HTTP.

- **robots.txt** parsed and cached per domain; a disallowed fetch raises rather
  than proceeding. A successful parse is cached for a day, a failure for five
  minutes, and an **unreachable** robots file is itself a refusal rather than a
  silent go-ahead. The one narrow exception is a non-crawl probe, a domain
  reachability check or a geocoding lookup, which may be waived explicitly
  rather than by default.
- **Per-domain pacing** with exponential back-off on 429/503 and relief on
  success. Concurrency capped by semaphore (NFR-102 asks for at least 20), with a
  negative-response cache and a fetch ledger so a repeated dead URL is not
  re-fetched.
- **Response cache** with a configurable TTL, so a repeat campaign does not
  re-fetch unchanged pages.
- **Raw capture (FR-183, DR-102):** every response is written to disk under a
  content-hash path and registered in `raw_document`, so extractors can be
  re-run when they improve.

### 3.3 LLM: `llm/client.py`

```python
llm.complete_json(
    "extract.vacancy",              # task id → routing, budget, audit
    system=..., user=...,
    untrusted={"page": html},       # fenced, never concatenated into instructions
    entity_type="vacancy", entity_id=vid,
)
```

- **Routing (FR-362, NFR-306).** Administrator settings beat `.env`; a per-task
  model override beats the cheap/strong split; privacy-sensitive tasks (composite profile, tailored CV, motivation document) route to a local
  OpenAI-compatible endpoint when one is configured. Settings are cached 30
  seconds and invalidated on save, so a change on the admin screen takes effect
  on the next call rather than the next restart.
- **Budget (NFR-104).** Checked before each call, debited after. `should_degrade()`
  lets the pipeline shed optional work, speculative openings for low-ranked
  companies first, instead of stopping mid-campaign.
- **Injection defence (NFR-205).** Scraped text is fenced in labelled untrusted
  blocks, fence delimiters neutralised, known injection phrasings annotated
  rather than removed (the model still sees the page, visibly marked). A system
  preamble states the rule. Output is validated before use.
- **Redaction (CR-410, FR-106, FR-127).** Do-not-disclose fields and
  special-category data are stripped before anything leaves the machine.
- **Audit (FR-364).** Task, template, version, model, provider, tokens, cost,
  latency and the related record are written to `llm_call`, with a scheduled
  redaction sweep.
- **Embeddings (FR-341).** `llm.embed()` writes one content-hash-keyed vector per
  entity into `embedding`, skipping unchanged content; nearest-neighbour search
  is pure-Python cosine, so the feature needs no vector database. It is
  backfilled by `scripts/build_semantic_index.py`, read by
  `GET /api/opportunities/semantic` (restricted to the seeker's own
  opportunities through `opportunities.vacancy_links`), and by scoring only
  when `DREAMJOB_SEMANTIC_SCORING` is on (`scoring.embedding_relevance`).

Prompts are versioned `.md` templates with a metadata header (NFR-602); the
version travels into `llm_call.prompt_version`, so a regression in generated
output can be tied to the prompt revision that caused it. There are **24
templates**, and the planner asks the cheap model first and escalates to the
strong model only when the first call fails to produce a usable plan.

### 3.4 Adapters: `adapters/base.py`

Four methods, and the pipeline calls nothing else:

```
plan(directives, composite, caps) → [PlanItem]      native query form
fetch(item)                       → [RawRecord]     through EgressClient
parse(raw)                        → [dict]          deterministic, LLM fallback
normalise(parsed, raw)            → NormalisedRecord knowledge-base columns
```

Each adapter declares coverage, capabilities, rate limit, cost and
terms-of-service status; `sync_catalogue()` writes that into `source_catalogue`
at boot, where the planner reads it (FR-161, FR-164) and the administrator can
override it (FR-363).

**36 adapters:** 9 ATS · 15 job boards · 6 registries · 1 directory · 1 website
crawler · 3 news/events · 1 compensation. Two bulk register importers
(`kbo_bulk.py`, `companies_house_bulk.py`) and the board registry deliberately
sit beside the contract rather than inside it: they seed the shared knowledge
base from an operator-run file, they do not collect per-campaign.

Extraction prefers JSON-LD `JobPosting` where present, then deterministic
selectors, then LLM extraction. Success rate is tracked per adapter so layout
breakage surfaces within one campaign (NFR-403), and a register's explicit "no"
is recorded as an answer rather than treated as a failure.

### 3.5 Jobs: `jobs/runner.py`

In-process asyncio. Each job checkpoints after every unit of work, so a crash
loses at most the page in flight (NFR-401). `checkpoint_barrier()` is where a
worker yields and honours pause, cancel and per-target skip. On boot, jobs left
running by a restart are marked resumable.

Deliberately not a broker: a broker buys nothing on a single node and breaks the
one-writer invariant.

---

## 4. Security

| Concern | Mechanism |
|---|---|
| Data at rest (NFR-201) | AES-256-GCM, keys derived per job seeker by HKDF from one master key. Credentials, secrets, TOTP material, sealed profile sections and package generation notes are field-encrypted. Deleting a job seeker makes their ciphertext unreadable even if a copy survives. |
| Passwords (NFR-202) | Argon2id (PBKDF2-SHA256 fallback), with a policy requiring three of four character classes |
| MFA (NFR-202) | TOTP, secret stored encrypted, fail-closed if the envelope cannot be decrypted |
| Passkeys (NFR-202) | WebAuthn via py_webauthn, user verification required, `none` attestation; public keys and sign counters in `job_seeker.webauthn_credentials`; challenges single-use, in memory, five minutes |
| Sessions (NFR-202) | Random token; only the HMAC is stored; bound to a UA+IP fingerprint and re-checked per request; revoked on password change, admin reset and suspension |
| Accounts (FR-108) | First account is admin; create/suspend/reset/delete, with a last-active-administrator guard and a self-guard |
| Third-party credentials (NFR-203) | **Never stored.** Browser automation attaches to a session the user owns |
| Mail tokens (NFR-204) | Encrypted, scoped to send + bounce-read, revocable from the UI; an `imap_basic` application password is encrypted the same way but is not scoped by the provider |
| Prompt injection (NFR-205) | Content isolation, instruction/data separation, output validation |
| Leakage (NFR-206) | Generated documents scanned against the job seeker's own provenance set |
| Objections (NFR-302) | Enforced in a `usable_contact` view, not only in the interface |
| Audit (NFR-702) | Append-only trail of who approved and sent what, with the versions used |

**Known gaps**, stated rather than glossed: passkeys work on `localhost`
unless the relying party is configured for another host, and their
attestation is not verified. The SQLite file itself is not
encrypted, so whole-database confidentiality rests on disk encryption,
SQLCipher would add a dependency.

---

## 5. Frontend

**React 19 + Vite 8 + react-router-dom v7.** Five dependencies in total: react,
react-dom, react-router-dom, vite, @vitejs/plugin-react. No UI kit, no chart
library, no date library, no state manager, charts are inline SVG, icons are
inline SVG, state is hooks plus one session context.

The shell presents the product as **three steps plus Advanced**: Start here,
1·Your profile, 2·Opportunities, 3·Apply, Follow up, with every deeper screen (directives, campaigns, companies, intelligence, networking, monitoring, mail,
admin) folded under one Advanced disclosure so a first-time user meets the
spine, not the whole surface.

**Route-level code splitting.** 24 lazy page chunks (26 async chunks including
the workflow map and the employer panel) behind a shared shell:

```
initial   318 KB JS + 59 KB CSS  (107 + 12 KB gzip)  shell + design system + router
per page  6-88 KB (2-24 KB gzip) loaded on navigation
```

Down from 858 KB unsplit, the administration area and the PDF previews no
longer load before the profile page can paint (NFR-101).

**Design system.** One `app.css` of tokens and layout, one `theme.css` that
re-points the tokens to the spectrum palette. Colour is structural: each of the
five journey phases owns a hue, so a violet screen is always about the profile
and an amber one is always about applying.

All six hues verified at **WCAG AA (≥4.5:1)** for small text against both the
surface and their own tint, in light and dark:

```
LIGHT        on white   on tint        DARK       on surface   on tint
Profile        5.37       4.63         Profile       6.39        5.92
Plan           5.19       4.56         Plan          7.10        6.25
Discover       5.13       4.51         Discover      8.40        6.99
Apply          5.05       4.52         Apply         8.51        7.49
Follow up      5.25       4.55         Follow up     7.19        6.63
```

Body text stays near-neutral; colour carries identity and state and never has to
be read. Speculative openings hold a hue outside all five phases, so FR-263
survives on colour alone. `prefers-reduced-motion` disables animation.

**Help is structural, not optional.** All copy lives in `help/content.js`, 21
screen entries and 142 glossary terms, so it is reviewable as a whole and
translatable as a unit (NFR-501). A screen cannot ship without help because the
drawer resolves its content from the route. **311 inline concept tips** across
the screens; `?` opens the panel anywhere.

---

## 6. Deployment

The quickstart is in the README; every other setting is in section 12.

Production is the same process serving the built SPA from `frontend/dist`.
Migrations run at startup. The **monitoring scheduler autostarts inside the API
process at boot** (controlled by `DREAMJOB_SCHEDULER_ENABLED`, with
`DREAMJOB_SCHEDULER_AUTOSTART` as an override) and runs twelve tasks, vacancy refresh,
job-alert e-mails, reply and bounce polling, watchlist rechecks, automatic company
enrichment, continuous collection (idle until switched on in Admin), geolocation,
follow-ups, outcome learning, the weekly digest, contact retention and LLM log
redaction.
`--once` remains available from an external cron, and any task can be stopped,
started or triggered from the monitoring screen.

`scripts/browser.sh` launches a Chromium-based browser with a remote debugging
port and a dedicated profile, for the collection steps that need a logged-in
session. A Firefox profile can be driven through a Playwright persistent context
instead, using the same session contract.

---

## 7. Data flow: one campaign

```
directives + composite profile + dream job model
        │
        ├─▶ knowledge base ──── reuse report (what is fresh enough already)
        │
        ▼
   plan per source ──▶ [user reviews, excludes, launches]
        │
        ▼
   JobRunner ──▶ adapters ──▶ EgressClient ──▶ raw_document
        │                          │
        │                          ▼
        │                     parse / normalise / dedup
        │                          │
        ▼                          ▼
   company resolution ──▶ shared knowledge base (domains, employer kind,
   domain ladder                 companies, vacancies, registry identity)
   employer-kind ladder              │
        │                            ▼
        ▼                     company enrichment (profile, signals,
   financial analysis            reviews) · financial analysis
        │
        ▼
   opportunity synthesis  (vacancies + speculative openings)
        │
        ▼
   scoring  ── deterministic pre-rank + LLM rationale for the top slice
        │
        ▼
   ranked list ──▶ [user selects]
        │
        ▼
   contacts ──▶ validation ──▶ generation ──▶ consistency check
        │                                            │
        │                                     [user approves]
        ▼                                            ▼
   audit trail ◀────────────────────────────── dispatch ──▶ replies
                                                               │
                                                               ▼
                                                     pipeline · segments ·
                                                     redirection advice
```

Every stage persists its artefacts and can be re-run independently (NFR-603).
The resolution ladders run before profiling so a posting is attributed to the
right company, and an agency or an undisclosed employer is labelled, not
profiled as if it were the client.

---

## 8. Performance

| Requirement | Approach |
|---|---|
| NFR-101 interactive views < 2s at p95 | 158 indexes; FTS5 for search; deterministic pre-rank avoids an LLM call per row; lazy routes keep first paint small |
| NFR-102 ≥20 concurrent HTTP workers, serialised writes | Semaphore-bounded async egress; one global write lock under WAL |
| NFR-103 typical campaign < 4 hours | Caps bound the work; ATS JSON is cheap; **not yet measured against a real campaign** |
| NFR-104 bounded LLM spend | Pre-flight check, post-call debit, graceful degradation |
| CR-406 browser automation is slow | Accepted by design: tens to low hundreds of targets, announced up front |

---

## 9. Testing

**2,595 tests collected, 72,107 lines across 153 files** (`tests/**/*.py`;
`-m 'not llm and not load'` deselects 14, leaving 2,581, of which the CI
command's `tests/unit tests/integration` runs 2,547. The offline pass count is
in `README.md`).
Each runs against a migrated temporary database and without network access; LLM
and HTTP paths are stubbed or skipped, and the network and load tests are
deselected. A Playwright e2e harness and persona generator
live under `tests/e2e` for the flows that need a browser.

The tests worth naming are the ones that encode a requirement rather than a
function:

- A private table added by a **later migration** is still exported and erased
  (FR-108), coverage is derived from the live schema, so a new table cannot be
  silently missed.
- Manual ranking survives recalculation (FR-284).
- A second job seeker's data appears nowhere in an export (FR-463).
- A redirection proposal naming an unseen segment is dropped, and effects are
  recomputed from the stored figures (§9.3 of the FDD).
- A professional history in a regulated domain is **not** stripped as
  special-category data, and a personal assertion **is** (FR-127).
- Administrator model settings actually change the routing decision (FR-362).
- Browser automation persists no cookie or token (NFR-203).
- An employer-kind verdict is never delivered without evidence, and a website
  rung's quotation is verified verbatim against the page (FR-341).
- A company register's explicit "no match" is recorded as an answer, not raised
  as a failure (FR-403).
- The last active administrator cannot be suspended or deleted (FR-108).
- A consistency override is persisted and reaches dispatch; a leak and an
  objected contact are not overridable (FR-322, NFR-206).

**Verified end-to-end against the real source documents**: LinkedIn export and
CV parsed and merged, 18 conflicts surfaced, photo extracted, 52 skills
normalised, and the dream-job model generated through the live DeepSeek API.

---

## 10. Extension points

| To add | Do this | Touching |
|---|---|---|
| A job board, registry or compensation source | Subclass `SourceAdapter`, `@register_adapter` | One file |
| An events or news source | Subclass the radar/news adapter, `@register_adapter` | One file |
| A jurisdiction's filings | A registry adapter + extractor mapping | Two files |
| A CV template | A module under `documents/templates/` | One file |
| A mail backend | Implement `MailBackend` | One file |
| A calendar provider | Implement the client and wire the OAuth exchange | One file |
| A language | Add to the prompt templates and `help/content.js` | Content only |
| A local model | Set `DREAMJOB_LOCAL_LLM_*` and list the task groups | Configuration |
| Semantic search | Set `DREAMJOB_EMBEDDINGS_*` and run `scripts/build_semantic_index.py`; `DREAMJOB_SEMANTIC_SCORING=true` to let it score | Config + script |
| A scheduler task | Add a `Task` with an interval to `DEFAULT_TASKS` | `monitoring/scheduler.py` |
| A bulk employer import | An operator script against the shared knowledge base | One script |
| An account lifecycle action | Add to `auth_service` behind `current_admin` | Service + router |
| PostgreSQL | Replace `db/connection.py` and the repository bodies | The layer built for it |

---

## 11. Requirement traceability

**156 of 157 requirements (99%)** are cited in the implementation, 103/104
Must, 44/44 Should, 9/9 Could. Regenerate the matrix with:

```bash
python3 scripts/traceability.py
```

The one uncited requirement is the DPIA itself (NFR-304), a document rather than
a code path. The known limitations are listed in the Functional Design, §12.


## 12. Setup, configuration and operation

Moved here from the README: everything an operator needs beyond the quickstart.

### 12.1 Sources

| Type | Count | Sources |
|---|---|---|
| ATS | 9 | Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Personio, Workday, Teamtailor, Workable |
| Job boards | 15 | EURES, VDAB, Actiris, Jobat, StepStone, Indeed, Welcome to the Jungle, Arbeitnow, generic HTML, Adzuna, Himalayas, Hacker News "Who is hiring", Jobicy, RemoteOK, Remotive |
| Registries | 6 | NBB, KBO/BCE, Companies House, KvK, SEC EDGAR, recherche-entreprises (FR) |
| Directories | 1 | OpenCorporates |
| Website | 1 | bounded company-site crawler |
| News / events | 3 | RSS newsrooms, event radar, social event calendars |
| Compensation | 1 | Eurostat Structure of Earnings Survey |

The counts above are rows in `source_catalogue`: **36 declared, 30 enabled**.
Indeed, StepStone and SmartRecruiters ship disabled until an administrator
acknowledges their terms of service (IR-101); `board.jobat`, `board.remotive`
and the social-event adapter (`events.social`) are enabled but also await that
acknowledgement, and `board.generic`,
`board.vdab` and `board.wttj` are declared and disabled.

**Contact discovery** does not stop at the company home page. It reads the
pages the home page links to, the site's **sitemap**, **schema.org JSON-LD**
and **`security.txt`**; it infers the domain's address convention from observed
addresses; and, for the large corporate sites that answer every fetch with an
F5/Cloudflare challenge, it can query a **search API** (Brave, Bing or Google
CSE, set it on the Contacts screen, *Search provider* tab, administrators
only; off until a key is supplied, because the engines disallow their HTML
search endpoint). The stored key is never sent back to the browser. The same
provider, once switched on, also serves profile enrichment's searches. A blocked site is recorded as
`blocked`, not reported as having no address.

### 12.2 Optional setup

**Browser automation**: for LinkedIn and Glassdoor, which block scrapers, and
for ATS form pre-fill. You launch your own browser and log in; the system
attaches and never sees your credentials.

```bash
./scripts/browser.sh          # dedicated profile, remote debugging port
```

> LinkedIn's user agreement prohibits automated access **including through a
> session you logged into yourself**. Your account may be restricted. Dream Job
> shows this warning before the first run and refuses to proceed until you
> acknowledge it. The decision is yours to make knowingly.

**Sending mail**: one of three backends:

- *Gmail OAuth*, set `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then connect on
  the Mail setup screen. Replies land in your own inbox and are detected.
- *Resend*, set `RESEND_API_KEY` and `DREAMJOB_MAIL_FROM` (an address on a domain you
  verified at Resend; it cannot send as a Gmail address). No inbox, so replies
  are recorded by webhook or entered by hand. Without a sender it reports itself
  unavailable.
- *IMAP + SMTP* (`imap_basic`), any mailbox that issues an application
  password (Yahoo, iCloud, ...), connected through `POST /api/mail/imap/connect`;
  replies land in that inbox. The Mail screen has no form for it yet.

**Google Calendar**: optional, for checking proposed interview slots against
your diary and creating the event. Set `GOOGLE_CALENDAR_CLIENT_ID`,
`GOOGLE_CALENDAR_CLIENT_SECRET` and list `GOOGLE_CALENDAR_REDIRECT_URI` on the
OAuth client, then `POST /api/pipeline/calendar/authorize` and open the URL it
returns. There is no button for it on a screen yet.

**Reading a Microsoft 365 / Outlook.com inbox**: read-only, for reply and
bounce detection: register an app as described in
[docs/M365_Mail_Setup.md](docs/M365_Mail_Setup.md), set `M365_CLIENT_ID`, then
connect through `GET /api/mail/m365/authorize`. It never sends.

`DREAMJOB_MAIL_DRY_RUN` defaults to `true`: the whole path runs, recipient,
guard rails, MIME with the CV attached, and stops one step short of the wire.
Arming a real send is a deliberate change of that setting plus a restart.

**A local model**: for the privacy-sensitive steps (composite profile, CV,
motivation document), any OpenAI-compatible endpoint:

```bash
DREAMJOB_LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
DREAMJOB_LOCAL_LLM_MODEL=llama3.1
DREAMJOB_LOCAL_LLM_TASKS=profile,cv,motivation
```

**Semantic search**: optional embeddings, inert when unset:

```bash
DREAMJOB_EMBEDDINGS_MODEL=text-embedding-3-small
DREAMJOB_EMBEDDINGS_BASE_URL=...
python3 scripts/build_semantic_index.py
```

Once the index is built, `GET /api/opportunities/semantic?q=...` ranks the
seeker's own opportunities by meaning. Scoring ignores the index unless
`DREAMJOB_SEMANTIC_SCORING=true`, which adds the vacancy's similarity to the
dream job as a fifth of the mechanical dream-fit sub-score (scores computed
with and without it are not comparable).

**Near-real-time vacancies**: on by default. The scheduler's `vacancy_refresh`
task runs every 10 minutes (`DREAMJOB_VACANCY_REFRESH_SECONDS`): it re-reads a
bounded, round-robin slice of the ATS boards behind existing ranked lists, and
each new posting is added to every following campaign under its directives,
scored, and announced once as a `new_vacancy` notification. Vacancy fetches
treat a cached board as fresh for one hour (`DREAMJOB_VACANCY_CACHE_TTL_SECONDS`);
after that it is revalidated, which costs a 0-byte 304 when nothing changed.

**Old vacancies are deleted.** Collection stores every posting it reads, but
only the ones a campaign retains become opportunities. Once a day the
`vacancy_purge` task deletes the vacancies no opportunity uses and nobody has
seen for the staleness window (7 days, never less), with their provenance and
embeddings. A vacancy behind an opportunity is kept at any age.
`DREAMJOB_VACANCY_PURGE_SECONDS=0` turns it off. To see what it would remove,
or to clear everything unlinked at once:

    PYTHONPATH=backend .venv/Scripts/python.exe scripts/purge_unlinked_vacancies.py
    PYTHONPATH=backend .venv/Scripts/python.exe scripts/purge_unlinked_vacancies.py --days 0 --apply

The first line only counts. `--apply` backs the database up next to itself first.

To discover beyond the boards already followed, turn on the continuous cycle in
**Admin → Continuous** with a 300 s interval. The UI receives updates live
through `/api/events/stream`.

The scheduler runs inside the API process and stops with it. On Windows, if it
must survive API restarts, set `DREAMJOB_SCHEDULER_ENABLED=0` and run
`python -m dreamjob.monitoring.scheduler --once` from Task Scheduler instead.

To know whether the job-alert mails in your mailbox can be deleted, run
`PYTHONPATH=backend python scripts/ingest_alerts.py`: it backs every alert mail
of the window up as `.eml` under `data/alerts_backup/`, runs one pass, and ends
with one line naming the cursor date before which deleting is safe.

### 12.3 With Docker (Docker Desktop)

One container holds the backend and the built React frontend:

```bash
cp .env.example .env          # fill DREAMJOB_MASTER_KEY, DREAMJOB_SESSION_SECRET and an LLM key
docker compose up --build     # builds the image "dreamjob-v2:local", then starts it
```

Open http://localhost:8000 (use `localhost`, not `127.0.0.1`: passkeys refuse an IP).
The container mounts the host's `data/` and `logs/`, so it sees the same database,
accounts and documents as the dev server. **Run one or the other, never both**:
SQLite locking is not reliable across the Windows/Docker boundary, and two
schedulers would collect into the same database. The container is never restarted
on its own (`restart: "no"`). The port is bound to 127.0.0.1 only and mail stays
in dry-run unless you change `DREAMJOB_MAIL_DRY_RUN` in `docker-compose.yml`.
Browser automation attaches to a Chrome on the host over CDP, which the container
cannot reach by default: use the non-Docker setup for that feature.

### 12.4 Configuration

All runtime configuration is read once from `.env` / the environment into a
frozen settings object, so the Admin screen can show what is in force; the only
direct `os.environ` reads are optional registry API keys,
`DREAMJOB_SCHEDULER_AUTOSTART` and `DREAMJOB_APPLY_ALLOW_SEND`. Groups include:

- **Core**: `DREAMJOB_ENV`, `DREAMJOB_HOST`, `DREAMJOB_PORT`, `DREAMJOB_DATA_DIR`, `DREAMJOB_DB_PATH`
- **Keys**: `DREAMJOB_MASTER_KEY`, `DREAMJOB_SESSION_SECRET`
- **Owner**: `DREAMJOB_OWNER_EMAIL`, `DREAMJOB_OWNER_NAME`, read by `scripts/bootstrap.py` and `scripts/backfill_scoring.py`
- **LLM**: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, cheap/strong model names, `DREAMJOB_LOCAL_LLM_*`, `DREAMJOB_EMBEDDINGS_*`, budget and cost
- **Mail**: `DREAMJOB_MAIL_BACKEND`, `DREAMJOB_MAIL_DRY_RUN`, `GMAIL_*`, `M365_*`, `RESEND_API_KEY`, `DREAMJOB_SEND_DAILY_CAP`, send window and interval
- **Egress**: user agent and the optional `DREAMJOB_CRAWLER_CONTACT`, per-domain RPS, concurrency, cache and negative-cache TTL, `DREAMJOB_VACANCY_CACHE_TTL_SECONDS` (freshness window for vacancy fetches), robots and crawl-delay flags, raw-document retention
- **Calendar**: `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI`
- **Browser**: `DREAMJOB_CDP_URL`, profile directory, human pacing bounds
- **Scheduler**: `DREAMJOB_SCHEDULER_ENABLED`, `DREAMJOB_SCHEDULER_TICK_SECONDS`, `DREAMJOB_VACANCY_REFRESH_SECONDS` (0 turns the vacancy refresh off), `DREAMJOB_VACANCY_PURGE_SECONDS` (0 turns the vacancy purge off)
- **Observability**: log directory, level, format, rotation, slow-request and slow-query thresholds

### 12.5 Adding a source

Adding a source is one file: subclass `SourceAdapter`, implement
`plan / fetch / parse / normalise`, decorate with `@register_adapter`. It appears
in the catalogue at the next boot. A language is content only, the prompt
templates and `help/content.js`.

### 12.6 Operator tooling

```bash
python3 scripts/bootstrap.py                 # schema + first administrator
python3 scripts/traceability.py              # requirement-to-file matrix
python3 scripts/build_semantic_index.py      # build the semantic index (SQLite table, or Qdrant with DREAMJOB_SEMANTIC_BACKEND=qdrant)

# Widen the company inventory (run by hand):
python3 scripts/import_board_registry.py     # public ATS boards, monthly
python3 scripts/resolve_employer_seed.py --dry-run   # named large employers
python3 scripts/import_register_bulk.py      # bulk company register seed
python3 scripts/ingest_kbo.py                # KBO staging
python3 scripts/mine_compensation.py         # salary corpus from postings
python3 scripts/normalise_corpus.py          # normalise collected corpus
python3 scripts/backfill_scoring.py          # rescore after a model change
python3 scripts/e2e_persona.py               # end-to-end persona run
```

The board registry is built from public URL indexes and shaped like them:
Personio and Recruitee SMEs, Teamtailor's Nordic base, and the Greenhouse and
Ashby boards named in Hacker News threads. The large employers a Benelux search
is judged on, the consultancies, the integrators, the telcos, run Workday or
their own careers site and appear in none of those indexes. The seed script
closes that gap from `backend/dreamjob/pipeline/data/employer_seed.json`: it
reads each named employer's own careers page and confirms the board with one
request before storing it. No slug is ever guessed.

### 12.7 Data protection in short

The product processes two kinds of personal data and treats them differently.

**Yours.** Encrypted at rest with a key derived for your account alone. Fields
you mark *do not disclose* are excluded from every generated document and
stripped before any prompt leaves the machine. Full export and complete erasure
are one call each, and erasure derives its table list from the live schema, so
a table added later cannot be silently missed.

**Hiring contacts'.** Only professional contact details are stored, kept to what
a professional introduction needs. Every introduction email offers a way to
object, and **an objection blocks that address permanently, for everyone** on the
installation, enforced in a database view rather than only in the interface.
Contacts gathered through browser automation expire with the campaign that
collected them (30 days by default) and are never shared between job seekers.

DeepSeek processes data outside the EU. You are told before any profile data is
sent, and your consent is recorded (CR-410). Privacy-sensitive steps can be
routed to a local model instead. See [DPIA.md](docs/DPIA.md).

### 12.8 Help inside the product

The **Where I am** screen is the map: sixteen stages across the five phases,
each showing what is done, what is running and, when something is blocked ,
*what would unblock it*. Every stage links to the screen that advances it, and a
one-line version of the map sits at the top of every working screen.

Press **`?`** on any screen for its instructions, or click the **?** beside any
label for that concept. 21 screens documented, 142 glossary terms, 310 inline
tips: the help is part of the product, not an afterthought.

---

*Companion documents: [Functional Design](Functional_Design.md) ·
[DPIA](DPIA.md) · [README](../README.md)*
