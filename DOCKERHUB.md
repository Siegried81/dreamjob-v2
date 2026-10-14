# dreamjob-v2

A job-search platform: live vacancies from ATS boards and job-alert e-mails,
employer profiles from public filings, and LLM-drafted applications that only
go out once a human approves them. FastAPI backend serving the built React UI
on port 8000. Source and documentation:
https://github.com/Siegried81/dreamjob-v2

```bash
docker run -d -p 127.0.0.1:8000:8000 --env-file .env \
  -v "$PWD/data:/app/data" siegried/dreamjob-v2
# open http://localhost:8000   (use "localhost", not 127.0.0.1: WebAuthn refuses IPs)
```

Copy `.env.example` from the repository and set at least
`DREAMJOB_MASTER_KEY`, `DREAMJOB_SESSION_SECRET` and an LLM key. Without a
master key the app still boots, but stored data is not encrypted. Mail is in
dry-run unless `DREAMJOB_MAIL_DRY_RUN=false` is set explicitly.

Tags: `latest` (main), `sha-<commit>`, and `<version>` for release tags.
