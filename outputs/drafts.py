"""Idempotent Gmail draft creation and sent reconciliation (spec section 14).

1. One transaction: insert `applications` row (drafting); company suppression is checked there.
2. Verify the resume file. 3. Build MIME with the PDF. 4. Create the draft, store ids -> drafted.
5. API error -> draft_failed (retried next run). 6. A crash leaves `drafting`; the next run looks
for a draft carrying our X-Job-Agent-Key header before creating another."""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from db import repo
from outputs.gmail_client import build_mime

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def _message(conn, app, gmail, cfg) -> object:
    draft = json.loads(app["draft_json"])
    pdf = Path(app["resume_pdf_path"])
    pdf = pdf if pdf.is_absolute() else ROOT / pdf
    if not pdf.is_file():
        raise FileNotFoundError(f"resume PDF missing: {app['resume_pdf_path']}")
    return build_mime(sender=gmail.address, to=app["recipient"], subject=draft["subject"], body=draft["body"],
                      job_key=app["job_key"], attachment=pdf, filename=cfg["outreach"]["attachment_filename"])


def _push(conn, app, gmail, cfg, now) -> str:
    """drafting -> drafted, or draft_failed with the error."""
    try:
        draft_id, thread_id = gmail.create_draft(_message(conn, app, gmail, cfg))
    except Exception as e:
        log.warning("draft failed for %s: %s", app["job_key"], e)
        repo.transition_application(conn, app["id"], "draft_failed", now, last_error=f"{type(e).__name__}: {e}"[:500])
        return "draft_failed"
    repo.transition_application(conn, app["id"], "drafted", now, draft_id=draft_id, thread_id=thread_id,
                                last_error=None)
    return "drafted"


def create_email_draft(conn, gmail, *, job: dict, draft, resume_key: str, resume_path: str,
                       pitch_variant: str | None, cfg: dict, now: datetime) -> str:
    """Returns drafted | draft_failed | suppressed | exists."""
    if conn.execute("SELECT 1 FROM applications WHERE job_key=?", (job["job_key"],)).fetchone():
        return "exists"
    app_id = repo.create_application(conn, job_key=job["job_key"], outreach_type="direct_email",
                                     bucket=job["bucket"], resume_key=resume_key, now=now,
                                     resume_pdf_path=resume_path, recipient=job["contact_email"],
                                     pitch_variant=pitch_variant)
    if app_id is None:
        return "suppressed"
    conn.execute("UPDATE applications SET draft_json=? WHERE id=?", (draft.model_dump_json(), app_id))
    app = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return _push(conn, app, gmail, cfg, now)


def recover(conn, gmail, cfg, now: datetime) -> dict:
    """Finish `drafting` rows left by a crash and retry `draft_failed` rows."""
    out = {"recovered": 0, "retried": 0, "failed": 0}
    for app in conn.execute("SELECT * FROM applications WHERE outreach_type='direct_email' "
                            "AND status IN ('drafting','draft_failed')").fetchall():
        if app["status"] == "drafting":
            found = gmail.find_draft(app["job_key"])
            if found:
                repo.transition_application(conn, app["id"], "drafted", now, draft_id=found[0], thread_id=found[1])
                out["recovered"] += 1
                continue
        else:
            repo.transition_application(conn, app["id"], "drafting", now)
        app = conn.execute("SELECT * FROM applications WHERE id=?", (app["id"],)).fetchone()
        result = _push(conn, app, gmail, cfg, now)
        out["retried" if result == "drafted" else "failed"] += 1
    return out


def reconcile(conn, gmail, now: datetime) -> dict:
    """Draft gone -> sent (company contacted) or discarded. Sent threads with a reply -> replied."""
    out = {"sent": 0, "discarded": 0, "replied": 0}
    for app in conn.execute("SELECT * FROM applications WHERE outreach_type='direct_email' "
                            "AND status='drafted'").fetchall():
        if gmail.draft_exists(app["draft_id"]):
            continue
        status = gmail.thread_status(app["thread_id"])
        if status["sent"]:
            sent_at = datetime.fromtimestamp(status["sent_at_ms"] / 1000, tz=timezone.utc)
            repo.transition_application(conn, app["id"], "sent", now, source="gmail", sent_at=repo.iso(sent_at))
            out["sent"] += 1
        else:
            repo.transition_application(conn, app["id"], "discarded", now, source="gmail")
            out["discarded"] += 1
    for app in conn.execute("SELECT * FROM applications WHERE outreach_type='direct_email' "
                            "AND status IN ('sent','no_response')").fetchall():
        if gmail.thread_status(app["thread_id"])["reply"]:
            repo.transition_application(conn, app["id"], "replied", now, source="gmail", outcome="replied")
            out["replied"] += 1
    return out


def cleanup_stale(conn, gmail, cfg: dict, now: datetime, liveness=None) -> dict:
    """Freshness check #3: unsent drafts older than recheck_draft_days are re-checked; a closed
    job or a draft older than expire_draft_days -> delete the draft, status expired."""
    from datetime import timedelta
    fu = cfg["followup"]
    out = {"expired": 0, "checked": 0}
    recheck = now - timedelta(days=fu["recheck_draft_days"])
    expire = now - timedelta(days=fu["expire_draft_days"])
    for app in conn.execute("SELECT a.*, j.sources, j.apply_url, j.last_seen_at, j.lifecycle FROM applications a "
                            "JOIN jobs j ON j.job_key=a.job_key WHERE a.outreach_type='direct_email' "
                            "AND a.status='drafted'").fetchall():
        created = repo.parse_iso(app["created_at"])
        if created > recheck:
            continue
        closed = app["lifecycle"] == "closed"
        if not closed and created > expire and liveness:
            out["checked"] += 1
            closed = liveness(dict(app)) == "closed"
            if closed:
                repo.update_job(conn, app["job_key"], {"lifecycle": "closed"})
        if closed or created <= expire:
            gmail.delete_draft(app["draft_id"])     # only the tool's own draft, never sent mail
            repo.transition_application(conn, app["id"], "expired", now)
            out["expired"] += 1
    return out


def mark_no_response(conn, now: datetime, days: int) -> int:
    """Sent with no reply `days` after sending -> no_response (run after reply detection)."""
    from datetime import timedelta
    cutoff = repo.iso(now - timedelta(days=days))
    rows = conn.execute("SELECT id FROM applications WHERE outreach_type='direct_email' AND status='sent' "
                        "AND sent_at < ?", (cutoff,)).fetchall()
    for r in rows:
        repo.transition_application(conn, r["id"], "no_response", now, outcome="no_response")
    return len(rows)
