"""SQLite access: connection + migrations, job rows, status transitions, company suppression."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-insert is atomic."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ---------------------------------------------------------------- migrations

def schema_version(conn) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not exists:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def migrate(conn) -> None:
    current = schema_version(conn)
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        number = int(re.match(r"(\d+)", path.name).group(1))
        if number <= current:
            continue
        script = path.read_text(encoding="utf-8")
        conn.executescript(f"BEGIN;\n{script}\nINSERT INTO schema_version (version) VALUES ({number});\nCOMMIT;")


# ---------------------------------------------------------------- jobs

JOB_COLUMNS = (
    "job_key", "company", "company_norm", "company_domain", "title", "title_norm",
    "canonical_url", "ats_key", "sources", "location_text", "description", "contact_email",
    "apply_url", "founder_links", "team_size", "posted_at", "posted_at_source",
    "first_seen_at", "last_seen_at", "lifecycle", "eligibility", "eligibility_reason",
    "bucket", "route", "status", "flags",
)


def insert_job(conn, row: dict) -> None:
    cols = [c for c in JOB_COLUMNS if c in row]
    conn.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])


def update_job(conn, job_key: str, fields: dict) -> None:
    if not fields:
        return
    sets = ", ".join(f"{c}=?" for c in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE job_key=?", [*fields.values(), job_key])


def get_job(conn, job_key: str):
    return conn.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()


def add_flag(conn, job_key: str, flag: str) -> None:
    row = get_job(conn, job_key)
    flags = json.loads(row["flags"] or "[]")
    if flag not in flags:
        flags.append(flag)
        update_job(conn, job_key, {"flags": json.dumps(flags)})


# ---------------------------------------------------------------- status transitions (section 16)

EMAIL_TRANSITIONS = {
    "drafting": {"drafted", "draft_failed"},
    "draft_failed": {"drafting", "drafted"},
    "drafted": {"sent", "discarded", "expired"},
    "sent": {"replied", "no_response"},
    "no_response": {"replied"},
    "replied": {"interview", "rejected"},
    "interview": {"offer", "rejected"},
}
PORTAL_TRANSITIONS = {
    "pending": {"applied", "discarded", "expired"},
    "applied": {"replied", "interview", "rejected", "offer"},
    "replied": {"interview", "rejected", "offer"},
    "interview": {"offer", "rejected"},
}
NETWORK_TRANSITIONS = {
    "to_connect": {"requested", "skipped"},
    "requested": {"connected", "skipped"},
}
TRANSITIONS = {"direct_email": EMAIL_TRANSITIONS, "application_brief": PORTAL_TRANSITIONS}

# Statuses that mean the outreach ended without contact (section 7). Anything else blocks.
UNBLOCKING = {
    "direct_email": {"discarded", "expired", "draft_failed"},
    "application_brief": {"discarded", "expired"},
}
CONTACT_STATUS = {"direct_email": "sent", "application_brief": "applied"}


class IllegalTransition(Exception):
    pass


def initial_status(outreach_type: str) -> str:
    return "drafting" if outreach_type == "direct_email" else "pending"


def _history(conn, kind: str, ref, old: str | None, new: str, now: datetime, source: str) -> None:
    conn.execute("INSERT INTO status_history (kind, ref, old_status, new_status, source, changed_at) "
                 "VALUES (?,?,?,?,?,?)", (kind, str(ref), old, new, source, iso(now)))


def transition_application(conn, app_id: int, new_status: str, now: datetime,
                           source: str = "tool", **fields) -> None:
    """Move an application to new_status (checked against the allowed transitions).
    Extra columns (draft_id, thread_id, last_error, ...) can be set in the same update."""
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    allowed = TRANSITIONS[app["outreach_type"]].get(app["status"], set())
    if new_status not in allowed:
        raise IllegalTransition(f"{app['outreach_type']}: {app['status']} -> {new_status}")
    if new_status == "sent":
        fields.setdefault("sent_at", iso(now))
    with transaction(conn):
        sets = "".join(f", {k}=?" for k in fields)
        conn.execute(f"UPDATE applications SET status=?, updated_at=?{sets} WHERE id=?",
                     [new_status, iso(now), *fields.values(), app_id])
        _history(conn, "application", app_id, app["status"], new_status, now, source)
        if new_status == CONTACT_STATUS[app["outreach_type"]]:
            _mark_contacted(conn, app["company_norm"], app["company_domain"], now)


def transition_network(conn, network_id: str, new_status: str, now: datetime, source: str = "tool") -> None:
    row = conn.execute("SELECT n.*, j.company_norm, j.company_domain FROM network n "
                       "JOIN jobs j ON j.job_key = n.job_key WHERE n.id=?", (network_id,)).fetchone()
    if new_status not in NETWORK_TRANSITIONS.get(row["status"], set()):
        raise IllegalTransition(f"network: {row['status']} -> {new_status}")
    with transaction(conn):
        conn.execute("UPDATE network SET status=?, updated_at=? WHERE id=?",
                     (new_status, iso(now), network_id))
        _history(conn, "network", network_id, row["status"], new_status, now, source)
        if new_status == "requested" and row["connection_note"]:
            _mark_contacted(conn, row["company_norm"], row["company_domain"], now)


# ---------------------------------------------------------------- company suppression (section 7)

def _company_match(prefix: str = "") -> str:
    """Same company if normalized names match, or both domains are known and match."""
    p = f"{prefix}." if prefix else ""
    return f"({p}company_norm = :norm OR (:domain IS NOT NULL AND {p}company_domain = :domain))"


def _mark_contacted(conn, company_norm: str, company_domain: str | None, now: datetime) -> None:
    conn.execute("INSERT OR IGNORE INTO companies_contacted (company_norm, company_domain, "
                 "first_contacted_at) VALUES (?, ?, ?)", (company_norm, company_domain, iso(now)))


def is_company_contacted(conn, company_norm: str, company_domain: str | None) -> bool:
    return conn.execute(f"SELECT 1 FROM companies_contacted WHERE {_company_match()}",
                        {"norm": company_norm, "domain": company_domain}).fetchone() is not None


def company_block_reason(conn, company_norm: str, company_domain: str | None,
                         exclude_job_key: str | None = None) -> str | None:
    """Return why a company is blocked from new outreach, or None if it is free."""
    params = {"norm": company_norm, "domain": company_domain, "exclude": exclude_job_key}
    if is_company_contacted(conn, company_norm, company_domain):
        return "contacted"
    for outreach_type, unblocking in UNBLOCKING.items():
        placeholders = ", ".join(f"'{s}'" for s in unblocking)
        hit = conn.execute(
            f"SELECT job_key, status FROM applications WHERE {_company_match()} "
            f"AND outreach_type = '{outreach_type}' AND status NOT IN ({placeholders}) "
            f"AND (:exclude IS NULL OR job_key != :exclude)", params).fetchone()
        if hit:
            return f"{outreach_type}:{hit['status']}:{hit['job_key']}"
    hit = conn.execute(
        f"SELECT n.job_key, n.status FROM network n JOIN jobs j ON j.job_key = n.job_key "
        f"WHERE {_company_match('j')} AND (n.status IN ('requested', 'connected') OR "
        f"(n.status = 'to_connect' AND COALESCE(n.connection_note, '') != '')) "
        f"AND (:exclude IS NULL OR n.job_key != :exclude)", params).fetchone()
    if hit:
        return f"network:{hit['status']}:{hit['job_key']}"
    return None


def create_application(conn, *, job_key: str, outreach_type: str, bucket: str,
                       resume_key: str, now: datetime, resume_pdf_path: str | None = None,
                       recipient: str | None = None, pitch_variant: str | None = None) -> int | None:
    """Create an outreach row unless the job already has one or its company is blocked.
    The check and the insert share one IMMEDIATE transaction (section 7)."""
    with transaction(conn):
        job = get_job(conn, job_key)
        if conn.execute("SELECT 1 FROM applications WHERE job_key=?", (job_key,)).fetchone():
            return None
        if company_block_reason(conn, job["company_norm"], job["company_domain"]):
            update_job(conn, job_key, {"status": "suppressed_company"})
            return None
        cur = conn.execute(
            "INSERT INTO applications (job_key, company_norm, company_domain, outreach_type, "
            "bucket, pitch_variant, resume_pdf_path, resume_key, status, recipient, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_key, job["company_norm"], job["company_domain"], outreach_type, bucket,
             pitch_variant, resume_pdf_path, resume_key, initial_status(outreach_type),
             recipient, iso(now), iso(now)))
        return cur.lastrowid


def create_network_entry(conn, *, job_key: str, person_id: int, connection_note: str | None,
                         now: datetime, after_accept_message: str | None = None) -> str | None:
    """A network row with a note is outreach and goes through the same company check."""
    with transaction(conn):
        job = get_job(conn, job_key)
        # The job's own email/kit doesn't block its LinkedIn note; another job's outreach does.
        if connection_note and company_block_reason(conn, job["company_norm"], job["company_domain"],
                                                    exclude_job_key=job_key):
            update_job(conn, job_key, {"status": "suppressed_company"})
            return None
        n = conn.execute("SELECT COUNT(*) FROM network WHERE job_key=?", (job_key,)).fetchone()[0]
        nid = f"{job_key}-{n + 1}"
        conn.execute("INSERT INTO network (id, job_key, person_id, connection_note, "
                     "after_accept_message, status, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,'to_connect',?,?)",
                     (nid, job_key, person_id, connection_note, after_accept_message,
                      iso(now), iso(now)))
        return nid
