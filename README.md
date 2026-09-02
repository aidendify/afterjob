# AfterJob

Free, self-hosted after-job review ask for local service owners. Import completed jobs, wait, send one CSAT question. Happy customers get your Google review link. Unhappy stay in a private inbox you run.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Web UI plus CSV import for completed jobs (customer name, email, optional job ref, completed time, notes, phone)
- Waits `DELAY_MINUTES` (default 90) after the job is marked done, then asks one CSAT question
- Copy the CSAT email, copy the token link, or optionally send through your own SMTP server
- Happy scores (default 4–5) show a single **Leave a Google review** button pointing at your `GOOGLE_REVIEW_URL`. No auto-redirect. Nothing is posted to Google.
- Unhappy scores stay in `/complaints`. AfterJob never asks those customers for a public review
- Generic inbound webhook `POST /hooks/jobs` for Zapier / n8n / your job software
- `GET /health` returns HTTP 200 JSON `{"status":"ok","smtp_configured":false}` even when SMTP is unset

The CSAT email is a template (no LLM). Sign-off uses `FROM_NAME` / `FROM_EMAIL` when set, otherwise `Your name`. The first email does **not** contain the Google review URL.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
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

```bash
git clone https://github.com/aidendify/afterjob.git
cd afterjob
cp .env.example .env
```

Edit `.env` and set at least `GOOGLE_REVIEW_URL`, `PUBLIC_BASE_URL`, `BUSINESS_NAME`, `SECRET_KEY`, `OWNER_PASSWORD`, and `WEBHOOK_SECRET`. Leave `SMTP_*` and `MARKETING_URL` empty unless you have mail. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

For a first smoke test set `DELAY_MINUTES=0` so you do not wait 90 minutes. The production default in `.env.example` stays `90`.

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `afterjob-data` volume at `/data/afterjob.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY`, `OWNER_PASSWORD`, `WEBHOOK_SECRET`, and your real Google review URL. Do not leave `DELAY_MINUTES=0` in production.

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

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"` and `"smtp_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass` if `OWNER_PASSWORD` is set, use **Import CSV**, and choose `sample-jobs.csv` from this repo. Do not `curl` the import route.

3. Open a job. Status should be `ready` (SMTP is unset, so send buttons stay hidden). Copy email / Copy link still work. The email body includes `http://localhost:8080/r/{token}` and does not include the Google review URL.

4. Optional webhook check (no `-L`):

   ```bash
   curl -sS -X POST http://localhost:8080/hooks/jobs \
     -H "Content-Type: application/json" \
     -H "X-AfterJob-Secret: test-secret" \
     -d '{"customer_name":"Webhook Pat","customer_email":"pat@example.com","job_ref":"WH-1"}'
   ```

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/afterjob.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Used in the CSAT email and pages. |
| `PUBLIC_BASE_URL` | No trailing slash. Used in copied/sent CSAT links, e.g. `http://localhost:8080`. |
| `GOOGLE_REVIEW_URL` | Owner’s write-a-review URL. Shown only after a happy CSAT score. Never posted-to. |
| `DELAY_MINUTES` | Wait after `completed_at` before the ask. Default 90. Use 0 for smoke tests. |
| `HAPPY_THRESHOLD` | Score ≥ this is happy (default 4). |
| `WEBHOOK_SECRET` | Auth for `POST /hooks/jobs`. Empty → webhook returns 403. |
| `FROM_NAME`, `FROM_EMAIL` | Sign-off and SMTP From. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional send. If `SMTP_HOST` is unset, send buttons are hidden. |
| `MARKETING_URL` | If set, footer link **Powered by AfterJob** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP passwords, `OWNER_PASSWORD`, and `WEBHOOK_SECRET` are never written to application logs.

## CSV import

Header row required. Columns recognized (case-insensitive):

- `customer_name` or `name` (required)
- `customer_email` or `email` (required)
- `completed_at` or `completed` (optional ISO-8601 or `YYYY-MM-DD`; default now UTC)
- `job_ref` or `job_id` (optional)
- `notes` (optional)
- `phone` (optional, stored, unused in this version)

Rows missing a name or a valid email are skipped. The import flash reports how many rows were imported vs skipped. Import from the browser; do not `curl -L` a POST.

## Webhook

`POST /hooks/jobs` accepts JSON with the same fields as the CSV. Auth is `Authorization: Bearer $WEBHOOK_SECRET` **or** `X-AfterJob-Secret: $WEBHOOK_SECRET`. If `WEBHOOK_SECRET` is empty, the endpoint returns 403.

Duplicate `job_ref` for the same email, or the same email plus the same `completed_at` minute, is idempotent and does not create a second ask.

## CSAT page

`GET /r/<token>` is public (no owner login). One question, buttons 1–5.

- Happy: thank-you plus **Leave a Google review** linking to `GOOGLE_REVIEW_URL`. No auto-redirect.
- Unhappy: “Sorry we missed the mark,” optional comment, then “We got it. We will not ask you for a public review.” No Google review link on this path.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. Health succeeds even when SMTP is unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./afterjob.db
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

## What this is not

AfterJob does not post to Google, talk to Jobber or Housecall Pro directly, send SMS, parse inbound email, or run as multi-tenant SaaS. It is a single Compose service you run yourself.
