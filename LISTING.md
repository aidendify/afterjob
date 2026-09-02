# AfterJob — marketplace listing

**Price:** Free (lead magnet). Self-hosted. No signup, no license server.

**One-liner:** After a service job is done, wait, then ask one CSAT question. Happy customers get your Google review link. Unhappy ones stay private.

**Who it's for:** Local service owners (HVAC, plumbing, roofing, and similar).

**Install:** About 15 minutes. Clone over HTTPS: `git clone https://github.com/aidendify/afterjob.git`. The repo is private until it is published; clone fails for people without access until then. Documented on Ubuntu 22.04/24.04. Debian 13 uses `docker.io` + `docker-compose`. Amazon Linux notes are not published yet.

**Smoke test:** `curl -sf http://localhost:8080/health` returns HTTP 200 and `{"smtp_configured":false,"status":"ok"}` when SMTP is unset (key order is `smtp_configured` then `status`). In the browser, log in if `OWNER_PASSWORD` is set, Import CSV (`sample-jobs.csv`), copy the CSAT email/link. Do not `curl -L` the CSV import.

**Repo:** https://github.com/aidendify/afterjob

**Config:** Copy `.env.example` to `.env`. Set `GOOGLE_REVIEW_URL`, `PUBLIC_BASE_URL`, `BUSINESS_NAME`, `SECRET_KEY`, `OWNER_PASSWORD`, `WEBHOOK_SECRET`. `PORT` is unused (always 8080). Leave `MARKETING_URL` empty. SMTP is optional.
