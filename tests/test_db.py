import pytest

from db import repo
from tests.conftest import NOW, make_job


def test_migrations_applied_once(conn):
    latest = len(list(repo.MIGRATIONS_DIR.glob("*.sql")))
    assert repo.schema_version(conn) == latest
    repo.migrate(conn)  # idempotent
    assert repo.schema_version(conn) == latest
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "evaluations", "applications", "network", "companies_contacted",
            "runs", "labels", "people", "dry_run_drafts", "sheet_queue", "status_history"} <= tables


def _app(conn, status="drafting", outreach_type="direct_email"):
    make_job(conn)
    app_id = repo.create_application(conn, job_key="src:hn:1", outreach_type=outreach_type,
                                     bucket="eligible", resume_key="fullstack", now=NOW)
    if status != repo.initial_status(outreach_type):
        conn.execute("UPDATE applications SET status=? WHERE id=?", (status, app_id))
    return app_id


@pytest.mark.parametrize("start,new", [
    ("drafting", "drafted"), ("drafted", "sent"), ("drafted", "discarded"),
    ("drafted", "expired"), ("drafting", "draft_failed"), ("draft_failed", "drafted"),
    ("sent", "replied"), ("replied", "interview"), ("interview", "offer"),
    ("sent", "no_response"),
])
def test_legal_email_transitions(conn, start, new):
    app_id = _app(conn, start)
    repo.transition_application(conn, app_id, new, NOW)
    assert conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()[0] == new


@pytest.mark.parametrize("start,new", [
    ("drafting", "sent"), ("sent", "drafted"), ("discarded", "sent"), ("expired", "drafted"),
    ("drafted", "offer"),
])
def test_illegal_email_transitions_raise(conn, start, new):
    app_id = _app(conn, start)
    with pytest.raises(repo.IllegalTransition):
        repo.transition_application(conn, app_id, new, NOW)


def test_portal_transitions(conn):
    app_id = _app(conn, "pending", outreach_type="application_brief")
    with pytest.raises(repo.IllegalTransition):
        repo.transition_application(conn, app_id, "sent", NOW)
    repo.transition_application(conn, app_id, "applied", NOW)
    assert repo.is_company_contacted(conn, "acme", None)


def test_sent_marks_company_contacted(conn):
    app_id = _app(conn, "drafted")
    assert not repo.is_company_contacted(conn, "acme", None)
    repo.transition_application(conn, app_id, "sent", NOW)
    assert repo.is_company_contacted(conn, "acme", None)


def test_status_changes_are_recorded(conn):
    app_id = _app(conn, "drafted")
    repo.transition_application(conn, app_id, "sent", NOW, source="gmail")
    rows = conn.execute("SELECT kind, old_status, new_status, source FROM status_history").fetchall()
    assert [tuple(r) for r in rows][-1] == ("application", "drafted", "sent", "gmail")
