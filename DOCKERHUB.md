# dreamjob-v2

A job-search platform: live vacancies from ATS boards and job-alert e-mails,
employer profiles from public filings, and LLM-drafted applications that only
go out once a human approves them. FastAPI backend serving the built React UI
on port 8000. Source and documentation:
https://github.com/Siegried81/dreamjob-v2

```bash
docker run -d -p 127.0.0.1:8000:8000 --env-file .env \
  -e DREAMJOB_DATA_DIR=/app/data -e DREAMJOB_DB_PATH=/app/data/dreamjob.db \
  -e DREAMJOB_LOG_DIR=/app/logs -e DREAMJOB_MAIL_DRY_RUN=true \
  -v "$PWD/data:/app/data" -v "$PWD/logs:/app/logs" siegried/dreamjob-v2
# open http://localhost:8000   (use "localhost", not 127.0.0.1: WebAuthn refuses IPs)
```

Copy `.env.example` from the repository and set at least
`DREAMJOB_MASTER_KEY`, `DREAMJOB_SESSION_SECRET` and an LLM key. Without a
master key the app still boots, but refuses to store anything sensitive
(credentials, sealed profile sections) rather than keep it unencrypted. An
existing `data/` can only be read with the master key it was written with.
Mail stays in dry-run unless `DREAMJOB_MAIL_DRY_RUN` is set to `false`.

Job-alert e-mails (LinkedIn, ictjob.be, Glassdoor, Jobat, Karimi) are read
from a Yahoo or other IMAP mailbox when `DREAMJOB_ALERTS_IMAP_USER` and
`DREAMJOB_ALERTS_IMAP_PASSWORD` (an app password) are set.

Run the container or the local dev server on a given `data/`, never both:
SQLite locking is not reliable across the Windows/Docker boundary.

Tags: `latest` (main), `sha-<commit>`, and `<version>` for release tags.
