<p align="center">
  <img src="docs/dreamjob-logo.png" alt="Dream Job" width="140">
</p>

# Dream Job

**AI-assisted job discovery and application platform.**

Dream Job helps you find work and apply for it. You say who you are and what
you want. It looks at the market, including companies that haven't advertised
anything, works out which ones can pay and are likely to hire, and prepares your
applications. It collects only what your directives justify, and nothing is sent
without your approval. It is built to the *Dream Job* Software Requirements
Specification v0.3.

Dream Job started as [stepvda/dreamjob](https://github.com/stepvda/dreamjob).
This repository is my own continuation, started from one snapshot (commit
`422d760`, 2026-10-06), so its history begins there. The snapshot carried 2,193
of the 2,547 tests the CI command runs today.

## What I built here

Everything below is in `git log 422d760..HEAD` (over 100 commits since the snapshot).
I added CI (ruff, tests, the frontend build) and Dependabot, the Windows fixes
(tzdata, line endings), the Dockerfile and the image publishing, an SSRF block in
the egress client and a security policy. On the product side: the `imap_basic`
mail backend, a read-only Microsoft 365 inbox, WebAuthn passkeys, job-alert
ingestion, distance search with a map, the French register adapter, a
search-provider form, the Qdrant and API side of the semantic index, vacancy
de-duplication and the stale-vacancy purge, and the employer-kind gates on the
research passes. The last audit added test network isolation, the wired
employer-kind helpers with a before and after measurement and its eval script,
Google Calendar OAuth, safe XML parsing, and a fix that keeps privacy-routed
LLM calls off the cloud fallback.

## What it does

- **Profile.** Merges your LinkedIn export and CV, shows every disagreement for
  you to resolve, and turns a free-text dream job into a structured model.
- **Discovery.** Collects from 30 enabled sources (ATS boards, job boards,
  registries, company sites), including unadvertised openings inferred from a
  company's finances and hiring signals.
- **Employers.** Tells an agency from an employer, and reads five years of
  filings into an ability-to-pay score and an investment-capacity score.
- **Applications.** Writes a tailored CV, a company briefing, a motivation
  document and an introduction email, checked for factual consistency. Mail
  defaults to dry-run.
- **Follow-up.** Records replies and outcomes, drafts answers, prepares
  interviews and a negotiation brief, and reports what actually works, with
  sample sizes.

React SPA, Python/FastAPI, SQLite (WAL), DeepSeek (OpenAI-compatible), Playwright
over CDP.

## Quickstart

Python 3.11+, Node 20.19+ (Vite 8) and a DeepSeek API key.

```bash
git clone https://github.com/Siegried81/dreamjob-v2.git && cd dreamjob-v2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..

cp .env.example .env
PYTHONPATH=backend python3 -m dreamjob.security.crypto --generate-key   # paste both values into .env
$EDITOR .env      # add DEEPSEEK_API_KEY, DREAMJOB_OWNER_EMAIL, DREAMJOB_OWNER_NAME

python3 scripts/bootstrap.py     # schema + the administrator named in DREAMJOB_OWNER_EMAIL
./scripts/dev.sh                 # API :8000, SPA :5173 with /api proxied
```

Open http://127.0.0.1:5173 (API reference at http://127.0.0.1:8000/docs). The
package lives under `backend/` and isn't installed by `requirements.txt`, so
every `python -m dreamjob...` command needs `PYTHONPATH=backend`; `scripts/dev.sh`
sets it. Migrations run at startup.

Mail is a dry run by default: messages are composed and stopped just before
sending. To send for real, set `DREAMJOB_MAIL_DRY_RUN=false` and connect a
mailbox (Gmail, or `DREAMJOB_MAIL_FROM` plus a Resend key). The Apply Browser
needs two more switches, `DREAMJOB_APPLY_ALLOW_SEND=true` in the environment and
the administration setting `apply.send_enabled`. Restart after changing the
environment.

Mail, a local model, semantic search,
Google Calendar, Docker and every setting are in
[Technical Architecture, section 12](docs/Technical_Architecture.md#12-setup-configuration-and-operation).

## Tests

```bash
PYTHONPATH=backend python3 -m pytest tests/unit tests/integration -q -m 'not llm and not load'
python3 -m ruff check backend/dreamjob
cd frontend && npm run build
python3 scripts/traceability.py
```

Tests run against a migrated temporary database and can't reach the network:
unit tests refuse every host but loopback. A Playwright e2e harness lives under
`tests/e2e` and needs the running app.

## Key numbers

2,532 of 2,547 tests pass offline, 15 skipped, none failing (the 15 need the
gitignored personal export and CV; 9 more skip until `frontend/dist` is built).
The run takes 9 to 15 minutes, depending on the machine. 247 Python modules (129,071 lines), 147 JS
modules (45,057 lines), 153 test files (72,107 lines), 23 routers and 364 API
paths, 101 tables, 158 indexes, 46 migrations, 36 source adapters (30 enabled),
24 versioned prompt templates, 21 help screens and 142 glossary terms. 156 of 157
specification requirements are cited in the source (103/104 Must, 44/44 Should,
9/9 Could); the one missing is the DPIA itself.

## Known limitations

- **Passkeys work on `localhost` only by default.** WebAuthn refuses an IP as
  relying-party id; set `DREAMJOB_WEBAUTHN_RP_ID` and `DREAMJOB_WEBAUTHN_ORIGINS`
  for another host. Attestation isn't checked, and a pending ceremony doesn't
  survive an API restart.
- **The SQLite file isn't encrypted.** Credentials, secrets and sealed profile
  sections are field-encrypted; the rest relies on disk encryption.
- **France** has revenue and net result only, no balance sheet, and nothing for
  companies with confidential accounts. **Germany** has no adapter: the
  Handelsregister has no official API and caps retrieval at 60 an hour.
- **Employer-kind enforcement covers new work, not old scores.** Opportunities
  scored before a company was tagged keep their company sub-score until they are
  re-scored.
- **Semantic scoring is off by default** and uncalibrated. The search endpoint
  is live once the index is built; the interface doesn't call it yet.
- **Microsoft 365 mail is read-only and API-only** (Inbox folder, replies and
  bounces). The Mail screen has no button for it, and job alerts are read from
  Gmail or IMAP only.
- **Google Calendar** connects through the API only; there is no screen for it.
- **Nothing is kept off the cloud by default.** `DREAMJOB_LOCAL_LLM_TASKS` is empty,
  so profile, CV and motivation calls go to DeepSeek, which processes data
  outside the EU. List those tasks and set `DREAMJOB_LOCAL_LLM_BASE_URL` to keep
  them on a local model; a task listed without a URL is refused, and a failed
  local call never falls back to the cloud.
- **Profile enrichment searches DuckDuckGo** unless a search provider is
  switched on, and DuckDuckGo returns a bot-check page from datacentre ranges.

## Docs

| | |
|---|---|
| [Functional Design](docs/Functional_Design.md) | What the product does, screen by screen |
| [Technical Architecture](docs/Technical_Architecture.md) | How it is built, plus setup, configuration and operation (section 12) |
| [DPIA](docs/DPIA.md) | Data-protection impact assessment (NFR-304) |
| [Collection outcome states](docs/Collection_Outcome_States.md) | What every collection result means |
| [Employer kind results](docs/Employer_Kind_Results.md) | Agency classification, measured |
| [Agency research design](docs/Agency_Research_Design.md), [interim agencies proposal](docs/Interim_Agencies_Proposal.md), [data gathering plan](docs/Data_Gathering_Plan.md) | The reasoning behind the employer-kind ladder and the collectors' rules, cited by section from the code |
| [Decisions](docs/decisions.md) | Choices the code alone doesn't explain |
| [Microsoft 365 mail setup](docs/M365_Mail_Setup.md) | The app registration a read-only inbox needs |
| [Security policy](SECURITY.md) | Reporting a vulnerability |

Word editions of the two design documents are generated from the markdown by
`docs/build_fdd.py` and `docs/build_ta.py`.

## Licence and status

Not yet production-deployed; the DPIA must be completed and reviewed first
(NFR-304).
