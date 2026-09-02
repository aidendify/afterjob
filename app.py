"""AfterJob: self-hosted after-job CSAT and review ask."""

from __future__ import annotations

import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
import smtplib
import threading
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "afterjob.db"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "afterjob-self-hosted-change-me")

_scheduler_lock = threading.Lock()
_scheduler_started = False

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUSES = ("queued", "ready", "sent", "happy", "unhappy", "skipped")
OPEN_ENDPOINTS = {"health", "login", "logout", "csat", "ingest_webhook", "static"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    if raw:
        return raw
    return str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def webhook_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "").strip()


def delay_minutes() -> int:
    raw = _env("DELAY_MINUTES") or "90"
    try:
        return max(0, int(raw))
    except ValueError:
        return 90


def happy_threshold() -> int:
    raw = _env("HAPPY_THRESHOLD") or "4"
    try:
        return int(raw)
    except ValueError:
        return 4


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def parse_completed_at(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return utc_now()
    raw = raw.replace("/", "-")
    if len(raw) >= 10:
        try:
            if raw[4] == "-" and raw[7] == "-" and (len(raw) == 10 or raw[10] in " T"):
                if len(raw) == 10:
                    d = date.fromisoformat(raw[:10])
                    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            pass
    iso = raw
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    except ValueError:
        return None


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def first_name(name: str) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else "there"


def signoff_block() -> str:
    name = _env("FROM_NAME")
    email = _env("FROM_EMAIL")
    if name and email:
        return f"{name}\n{email}"
    if name:
        return name
    if email:
        return email
    return "Your name"


def csat_link(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/r/{token}"


def csat_subject(job: sqlite3.Row | dict) -> str:
    name = job["customer_name"] if not isinstance(job, dict) else job["customer_name"]
    return f"How did we do, {first_name(name)}?"


def csat_body(job: sqlite3.Row | dict) -> str:
    name = job["customer_name"] if not isinstance(job, dict) else job["customer_name"]
    token = job["token"] if not isinstance(job, dict) else job["token"]
    fn = first_name(name)
    business = _env("BUSINESS_NAME") or "us"
    link = csat_link(token)
    sign = signoff_block()
    return (
        f"Hi {fn},\n\n"
        f"Thank you for choosing {business}. The job is done, and we have one question: "
        f"how did we do?\n\n"
        f"{link}\n\n"
        f"It takes about ten seconds.\n\n"
        f"Thank you,\n{sign}\n"
    )


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_exc: BaseException | None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                phone TEXT,
                job_ref TEXT,
                notes TEXT,
                completed_at TEXT NOT NULL,
                send_at TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                score INTEGER,
                sent_at TEXT,
                responded_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL UNIQUE,
                score INTEGER NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_send_at ON jobs(status, send_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_token ON jobs(token);
            CREATE INDEX IF NOT EXISTS idx_jobs_email ON jobs(customer_email);
            """
        )
        db.commit()


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": _env("MARKETING_URL"),
        "smtp_configured": smtp_configured(),
        "business_name": _env("BUSINESS_NAME") or "AfterJob",
        "owner_locked": bool(owner_password()),
        "logged_in": bool(session.get("owner")) or not owner_password(),
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def secrets_equal(provided: str, expected: str) -> bool:
    a = (provided or "").encode("utf-8")
    b = (expected or "").encode("utf-8")
    if len(a) != len(b):
        return hmac.compare_digest(b, b) and False
    return hmac.compare_digest(a, b)


def get_job(job_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_token(token: str) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM jobs WHERE token = ?", (token,)).fetchone()


def minute_prefix(iso_str: str) -> str:
    return (iso_str or "")[:16]


def find_duplicate(conn: sqlite3.Connection, email: str, job_ref: str, completed_at: str) -> sqlite3.Row | None:
    email_l = email.strip().lower()
    if job_ref:
        row = conn.execute(
            "SELECT * FROM jobs WHERE lower(customer_email) = ? AND job_ref = ?",
            (email_l, job_ref),
        ).fetchone()
        if row is not None:
            return row
    prefix = minute_prefix(completed_at)
    return conn.execute(
        "SELECT * FROM jobs WHERE lower(customer_email) = ? AND substr(completed_at, 1, 16) = ?",
        (email_l, prefix),
    ).fetchone()


def insert_job(data: dict) -> tuple[sqlite3.Row, bool]:
    """Insert a job or return the existing duplicate. (row, created)."""
    conn = get_db()
    existing = find_duplicate(
        conn,
        data["customer_email"],
        data.get("job_ref") or "",
        data["completed_at"],
    )
    if existing is not None:
        return existing, False
    token = secrets.token_hex(32)
    completed = data["completed_at"]
    completed_dt = parse_completed_at(completed) or utc_now()
    send_at = to_iso(completed_dt + timedelta(minutes=delay_minutes()))
    cur = conn.execute(
        """
        INSERT INTO jobs (
            customer_name, customer_email, phone, job_ref, notes,
            completed_at, send_at, token, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (
            data["customer_name"],
            data["customer_email"].strip(),
            data.get("phone") or None,
            data.get("job_ref") or None,
            data.get("notes") or None,
            completed,
            send_at,
            token,
            utc_now_iso(),
        ),
    )
    conn.commit()
    job_id = int(cur.lastrowid)
    claim_and_dispatch(conn, job_id)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return job, True


def claim_and_dispatch(conn: sqlite3.Connection, job_id: int) -> None:
    now = utc_now_iso()
    target = "sent" if smtp_configured() else "ready"
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = ?
        WHERE id = ? AND status = 'queued' AND send_at <= ?
        """,
        (target, job_id, now),
    )
    conn.commit()
    if cur.rowcount != 1:
        return
    if not smtp_configured():
        return
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return
    try:
        send_csat_email(job)
        conn.execute(
            "UPDATE jobs SET sent_at = ? WHERE id = ?",
            (now, job_id),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — do not log SMTP credentials
        app.logger.warning("SMTP send failed for job_id=%s: %s", job_id, type(exc).__name__)
        conn.execute(
            "UPDATE jobs SET status = 'queued', sent_at = NULL WHERE id = ? AND status = 'sent'",
            (job_id,),
        )
        conn.commit()


def process_due_jobs() -> None:
    conn = get_db()
    now = utc_now_iso()
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status = 'queued' AND send_at <= ? ORDER BY id ASC",
        (now,),
    ).fetchall()
    for row in rows:
        claim_and_dispatch(conn, row["id"])


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def send_csat_email(job: sqlite3.Row) -> None:
    send_smtp(job["customer_email"], csat_subject(job), csat_body(job))


def normalize_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower().replace("_", " ").replace("-", " "))


HEADER_MAP = {
    "customer name": "customer_name",
    "name": "customer_name",
    "customer email": "customer_email",
    "email": "customer_email",
    "e mail": "customer_email",
    "completed at": "completed_at",
    "completed": "completed_at",
    "job ref": "job_ref",
    "job id": "job_ref",
    "notes": "notes",
    "note": "notes",
    "phone": "phone",
}


def parse_job_fields(source: dict) -> dict | None:
    mapped: dict[str, str] = {}
    for key, value in source.items():
        if key is None:
            continue
        field = HEADER_MAP.get(normalize_header(str(key)))
        if field and field not in mapped:
            mapped[field] = ("" if value is None else str(value)).strip()
    name = mapped.get("customer_name", "")
    email = mapped.get("customer_email", "")
    if not name or not valid_email(email):
        return None
    completed_raw = mapped.get("completed_at", "")
    completed_dt = parse_completed_at(completed_raw) if completed_raw else utc_now()
    if completed_dt is None:
        return None
    return {
        "customer_name": name,
        "customer_email": email,
        "phone": mapped.get("phone", ""),
        "job_ref": mapped.get("job_ref", ""),
        "notes": mapped.get("notes", ""),
        "completed_at": to_iso(completed_dt),
    }


def extract_webhook_secret() -> str:
    header = (request.headers.get("X-AfterJob-Secret") or "").strip()
    if header:
        return header
    auth = request.headers.get("Authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def _scheduler_loop() -> None:
    while True:
        try:
            with app.app_context():
                process_due_jobs()
        except Exception:
            app.logger.exception("scheduler tick failed")
        time.sleep(15)


def start_scheduler() -> None:
    global _scheduler_started
    flag = _env("AFTERJOB_DISABLE_SCHEDULER").lower()
    if flag in {"1", "true", "yes", "on"}:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="afterjob-scheduler", daemon=True)
    thread.start()


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "smtp_configured": smtp_configured()})


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    if request.method == "POST":
        provided = request.form.get("password") or ""
        if secrets_equal(provided, owner_password()):
            session["owner"] = True
            return redirect(nxt)
        flash("Wrong password.", "error")
    return render_template("login.html", next=nxt, public=True)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if owner_password() else url_for("index"))


@app.get("/")
def index() -> str:
    status = (request.args.get("status") or "").strip().lower()
    db = get_db()
    if status in STATUSES:
        jobs = db.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC",
            (status,),
        ).fetchall()
    else:
        status = ""
        jobs = db.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    counts_rows = db.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["n"] for row in counts_rows}
    counts["all"] = sum(counts.values())
    return render_template("index.html", jobs=jobs, status=status, counts=counts)


@app.post("/jobs")
def create_job():
    fields = parse_job_fields(
        {
            "customer_name": request.form.get("customer_name") or request.form.get("name") or "",
            "customer_email": request.form.get("customer_email") or request.form.get("email") or "",
            "phone": request.form.get("phone") or "",
            "job_ref": request.form.get("job_ref") or "",
            "notes": request.form.get("notes") or "",
            "completed_at": request.form.get("completed_at") or "",
        }
    )
    if fields is None:
        flash("Name and a valid email are required. Check the completed date if you set one.", "error")
        return redirect(url_for("index"))
    job, created = insert_job(fields)
    if created:
        flash("Job added.", "ok")
    else:
        flash("That job was already on file (same email and job ref, or same email and completed minute).", "ok")
    return redirect(url_for("job_detail", job_id=job["id"]))


@app.post("/jobs/import")
def import_jobs():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("index"))
    raw = upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        flash("CSV is missing a header row.", "error")
        return redirect(url_for("index"))
    imported = 0
    skipped = 0
    for row in reader:
        mapped = parse_job_fields(row)
        if mapped is None:
            skipped += 1
            continue
        _job, created = insert_job(mapped)
        if created:
            imported += 1
        else:
            skipped += 1
    flash(f"Imported {imported} job(s), skipped {skipped} row(s).", "ok")
    return redirect(url_for("index"))


@app.get("/jobs/<int:job_id>")
def job_detail(job_id: int) -> str:
    job = get_job(job_id)
    if job is None:
        abort(404)
    return render_template(
        "job.html",
        job=job,
        email_subject=csat_subject(job),
        email_body=csat_body(job),
        csat_url=csat_link(job["token"]),
    )


@app.post("/jobs/<int:job_id>/skip")
def skip_job(job_id: int):
    job = get_job(job_id)
    if job is None:
        abort(404)
    if job["status"] in {"happy", "unhappy"}:
        flash("This customer already responded.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    db = get_db()
    db.execute(
        "UPDATE jobs SET status = 'skipped' WHERE id = ? AND status IN ('queued', 'ready', 'sent')",
        (job_id,),
    )
    db.commit()
    flash("Marked as skipped. No ask will be sent.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/mark-sent")
def mark_sent(job_id: int):
    job = get_job(job_id)
    if job is None:
        abort(404)
    if job["status"] not in {"queued", "ready"}:
        flash("Only queued or ready jobs can be marked sent.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    db = get_db()
    db.execute(
        "UPDATE jobs SET status = 'sent', sent_at = ? WHERE id = ? AND status IN ('queued', 'ready')",
        (utc_now_iso(), job_id),
    )
    db.commit()
    flash("Marked as sent.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/send")
def send_now(job_id: int):
    job = get_job(job_id)
    if job is None:
        abort(404)
    if not smtp_configured():
        flash("SMTP is not configured. Copy the email or mark sent instead.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    if job["status"] not in {"queued", "ready"}:
        flash("This job is not waiting to send.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    db = get_db()
    try:
        send_csat_email(job)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("SMTP send failed for job_id=%s: %s", job_id, type(exc).__name__)
        flash("SMTP send failed.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    db.execute(
        "UPDATE jobs SET status = 'sent', sent_at = ? WHERE id = ? AND status IN ('queued', 'ready')",
        (utc_now_iso(), job_id),
    )
    db.commit()
    flash("Sent the CSAT email.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/complaints")
def complaints() -> str:
    rows = get_db().execute(
        """
        SELECT c.id, c.job_id, c.score, c.comment, c.created_at,
               j.customer_name, j.customer_email, j.job_ref
        FROM complaints c
        JOIN jobs j ON j.id = c.job_id
        ORDER BY c.id DESC
        """
    ).fetchall()
    return render_template("complaints.html", complaints=rows)


@app.post("/hooks/jobs")
def ingest_webhook():
    secret = webhook_secret()
    if not secret:
        return jsonify({"error": "webhook disabled"}), 403
    provided = extract_webhook_secret()
    if not provided or not secrets_equal(provided, secret):
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid json"}), 400
    fields = parse_job_fields(payload)
    if fields is None:
        return jsonify({"error": "name and email are required"}), 400
    job, created = insert_job(fields)
    body = {"ok": True, "id": job["id"], "status": job["status"], "created": created}
    return jsonify(body), 201 if created else 200


def _complaint_for(job_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM complaints WHERE job_id = ?",
        (job_id,),
    ).fetchone()


def _record_score(job: sqlite3.Row, score: int, comment: str) -> sqlite3.Row:
    db = get_db()
    threshold = happy_threshold()
    now = utc_now_iso()
    status = "happy" if score >= threshold else "unhappy"
    cur = db.execute(
        """
        UPDATE jobs
        SET status = ?, score = ?, responded_at = ?
        WHERE id = ? AND status IN ('queued', 'ready', 'sent')
        """,
        (status, score, now, job["id"]),
    )
    db.commit()
    if cur.rowcount == 1 and status == "unhappy":
        db.execute(
            """
            INSERT OR IGNORE INTO complaints (job_id, score, comment, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job["id"], score, comment or None, now),
        )
        db.commit()
    elif cur.rowcount != 1 and status == "unhappy" and comment:
        db.execute(
            """
            UPDATE complaints SET comment = ?
            WHERE job_id = ? AND (comment IS NULL OR comment = '')
            """,
            (comment, job["id"]),
        )
        db.commit()
    return get_job(job["id"])  # type: ignore[return-value]


@app.route("/r/<token>", methods=["GET", "POST"])
def csat(token: str):
    job = get_job_by_token(token)
    if job is None:
        abort(404)
    google_url = _env("GOOGLE_REVIEW_URL")
    if request.method == "POST":
        existing_status = job["status"]
        if existing_status in {"happy", "unhappy"}:
            comment = (request.form.get("comment") or "").strip()
            if existing_status == "unhappy" and comment:
                db = get_db()
                db.execute(
                    """
                    UPDATE complaints SET comment = ?
                    WHERE job_id = ? AND (comment IS NULL OR comment = '')
                    """,
                    (comment, job["id"]),
                )
                db.commit()
        else:
            raw_score = (request.form.get("score") or "").strip()
            try:
                score = int(raw_score)
            except ValueError:
                score = 0
            if score in {1, 2, 3, 4, 5}:
                comment = (request.form.get("comment") or "").strip()
                job = _record_score(job, score, comment)
            else:
                flash("Please choose a score from 1 to 5.", "error")
        job = get_job_by_token(token)

    status = job["status"]
    complaint = _complaint_for(job["id"]) if status == "unhappy" else None
    comment_done = bool(complaint and (complaint["comment"] or "").strip()) if complaint else False
    if status == "happy":
        mode = "happy"
    elif status == "unhappy":
        mode = "unhappy"
    else:
        mode = "ask"
    return render_template(
        "csat.html",
        job=job,
        mode=mode,
        google_review_url=google_url if mode == "happy" else "",
        comment_done=comment_done,
        public=True,
    )


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


init_db()
start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(_env("PORT") or "8080"))
