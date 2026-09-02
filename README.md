# AfterJob

Free, self-hosted review ask for local service owners (HVAC, plumbing, roofing, and similar). After a job is done, wait a bit, then send one CSAT question. Happy customers get your Google review link. Unhappy ones stay in a private inbox on a machine you run.

No signup. No license. One Docker app and a SQLite file. Plan on about 15 minutes on a 1GB VPS.

## What you get

- A simple web page for completed jobs (customer name, email, optional job ref, completed time, notes, phone), including CSV import
- Waits `DELAY_MINUTES` (default 90) after the job is marked done, then asks one CSAT question
- Copy the CSAT email, copy the token link, or optionally send through **your** SMTP server
- Happy scores (default 4–5) show a **Leave a Google review** button pointing at your `GOOGLE_REVIEW_URL`. No auto-redirect. Nothing is posted to Google.
- Unhappy scores stay in `/complaints`. AfterJob never asks those customers for a public review
- Generic inbound webhook `POST /hooks/jobs` for Zapier / n8n / your job software

The CSAT email is a template (no AI). Sign-off uses `FROM_NAME` / `FROM_EMAIL` when set, otherwise `Your name`. The first email does **not** contain the Google review URL.

## What you need

- A small VPS (about 1GB RAM is enough)
- **Ubuntu 22.04 or 24.04** is the documented install. Debian 13 is covered with a different Docker package set below. Amazon Linux is not documented in this README yet
- Port 8080 open to you (and to the internet only if you want the UI reachable from outside)
- Your Google "write a review" URL

## 15-minute install (Ubuntu 22.04 / 24.04)

### 1. Install Docker Engine and Compose

This recipe is for Ubuntu. Do not run it unchanged on Debian or Amazon Linux.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

Clone over **HTTPS** (not SSH):

```bash
git clone https://github.com/aidendify/afterjob.git
cd afterjob
cp .env.example .env
```

Edit `.env` and set at least `GOOGLE_REVIEW_URL`, `PUBLIC_BASE_URL`, `BUSINESS_NAME`, `SECRET_KEY`, `OWNER_PASSWORD`, and `WEBHOOK_SECRET`. Leave `SMTP_*` and `MARKETING_URL` empty unless you have mail. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

`PORT` in `.env.example` is unused. The container always listens on 8080.

For a first smoke test set `DELAY_MINUTES=0` so you do not wait 90 minutes. The production default in `.env.example` stays `90`.

```bash
docker compose up --build -d
```

The app listens on port 8080. Data lives in a Docker volume (`afterjob-data`) at `/data/afterjob.db` inside the container.

If the image build fails with a 502 from debian.org while installing `curl`, wait a minute and run `docker compose up --build -d` again.

### 3. Smoke test

Use this `.env` for a first pass. Production should use a real `SECRET_KEY`, `OWNER_PASSWORD`, `WEBHOOK_SECRET`, and your real Google review URL. Do not leave `DELAY_MINUTES=0` in production.

```
DELAY_MINUTES=0
OWNER_PASSWORD=testpass
WEBHOOK_SECRET=test-secret
GOOGLE_REVIEW_URL=https://example.com/google-review-test
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` unset.

1. Health check (SMTP does not need to be configured):

   ```bash
   curl -sf http://localhost:8080/health
   ```

   You should get HTTP 200 and JSON like:

   ```json
   {"smtp_configured":false,"status":"ok"}
   ```

   Key order is `smtp_configured` then `status`. `smtp_configured` is `true` only when `SMTP_HOST` is set.

2. Open http://localhost:8080, log in with `testpass` if `OWNER_PASSWORD` is set, click **Import CSV**, and choose `sample-jobs.csv` from this repo. Do not `curl -L` the import URL.

3. Open a job. Status should be `ready` (SMTP is unset, so send buttons stay hidden). Copy email / Copy link still work. The email body includes `http://localhost:8080/r/{token}` and does not include the Google review URL.

4. Optional webhook check (no `-L`):

   ```bash
   curl -sS -X POST http://localhost:8080/hooks/jobs \
     -H "Content-Type: application/json" \
     -H "X-AfterJob-Secret: test-secret" \
     -d '{"customer_name":"Webhook Pat","customer_email":"pat@example.com","job_ref":"WH-1"}'
   ```

## Debian 13

Use Debian's `docker.io` and `docker-compose` packages, not the Ubuntu `docker-ce` recipe above.

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates docker.io docker-compose
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker
```

Log out and back in (or `newgrp docker`), then clone over HTTPS as above and start with:

```bash
docker-compose up --build -d
```

Smoke test is the same as Ubuntu.

## Amazon Linux

Amazon Linux install steps are not in this README yet. Use Ubuntu 22.04/24.04 or Debian 13 for now.

## Configuration

Copy `.env.example` to `.env` before starting. Do not commit `.env`.

| Variable | Purpose |
| --- | --- |
| `PORT` | Unused. The container always binds gunicorn to `0.0.0.0:8080`. Changing this variable does not change the listen port. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/afterjob.db`. |
| `SECRET_KEY` | Change this on any VPS that is reachable from the internet. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Used in the CSAT email and pages. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in copied/sent CSAT links, e.g. `http://localhost:8080`. |
| `GOOGLE_REVIEW_URL` | Your write-a-review URL. Shown only after a happy CSAT score. Never posted-to. |
| `DELAY_MINUTES` | Wait after `completed_at` before the ask. Default 90. Use 0 for smoke tests. |
| `HAPPY_THRESHOLD` | Score ≥ this is happy (default 4). |
| `WEBHOOK_SECRET` | Auth for `POST /hooks/jobs`. Empty → webhook returns 403. |
| `FROM_NAME`, `FROM_EMAIL` | Sign-off and SMTP From. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional send. If `SMTP_HOST` is unset, send buttons are hidden. |
| `MARKETING_URL` | If set, a footer link **Powered by AfterJob** points here. Leave empty for no footer. |

SMTP passwords, `OWNER_PASSWORD`, and `WEBHOOK_SECRET` are never written to application logs.

## CSV import

Use **Import CSV** in the browser. Header row required. Columns recognized (case-insensitive):

- `customer_name` or `name` (required)
- `customer_email` or `email` (required)
- `completed_at` or `completed` (optional ISO-8601 or `YYYY-MM-DD`; default now UTC)
- `job_ref` or `job_id` (optional)
- `notes` (optional)
- `phone` (optional, stored, unused in this version)

Rows missing a name or a valid email are skipped. The import message reports how many rows were imported vs skipped.

Do not import by running `curl -L` against the import URL.

## Webhook

`POST /hooks/jobs` accepts JSON with the same fields as the CSV. Auth is `Authorization: Bearer $WEBHOOK_SECRET` **or** `X-AfterJob-Secret: $WEBHOOK_SECRET`. If `WEBHOOK_SECRET` is empty, the endpoint returns 403.

Duplicate `job_ref` for the same email, or the same email plus the same `completed_at` minute, is idempotent and does not create a second ask.

## CSAT page

`GET /r/<token>` is public (no owner login). One question, buttons 1–5.

- Happy: thank-you plus **Leave a Google review** linking to `GOOGLE_REVIEW_URL`. No auto-redirect.
- Unhappy: “Sorry we missed the mark,” optional comment, then “We got it. We will not ask you for a public review.” No Google review link on this path.

## Healthcheck

`GET /health` returns HTTP 200 even when SMTP is unset:

```json
{"smtp_configured":false,"status":"ok"}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./afterjob.db
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code. The supported install is Docker Compose.

## What this is not

AfterJob does not post to Google, talk to Jobber or Housecall Pro directly, send SMS, parse inbound email, or run as multi-tenant SaaS. It is a single Compose service you run yourself.
