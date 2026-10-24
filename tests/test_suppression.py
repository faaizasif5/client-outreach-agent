"""Company suppression (spec section 7): one active outreach per company across email,
portal and LinkedIn; unblocked only when every outreach ended without contact."""
import pytest

from db import repo
from tests.conftest import NOW, make_job


def _email(conn, key, company_norm="acme", domain=None):
    make_job(conn, job_key=key, company_norm=company_norm, company_domain=domain)
    return repo.create_application(conn, job_key=key, outreach_type="direct_email",
                                   bucket="eligible", resume_key="fullstack", now=NOW)


def _set(conn, app_id, status):
    conn.execute("UPDATE applications SET status=? WHERE id=?", (status, app_id))


@pytest.mark.parametrize("status", ["drafting", "drafted", "sent", "replied", "interview",
                                    "offer", "rejected", "no_response"])
def test_email_statuses_block(conn, status):
    _set(conn, _email(conn, "k1"), status)
    assert repo.company_block_reason(conn, "acme", None)
    assert _email(conn, "k2") is None
    row = conn.execute("SELECT status FROM jobs WHERE job_key='k2'").fetchone()
    assert row[0] == "suppressed_company"


@pytest.mark.parametrize("status", ["discarded", "expired", "draft_failed"])
def test_email_ended_without_contact_unblocks(conn, status):
    _set(conn, _email(conn, "k1"), status)
    assert repo.company_block_reason(conn, "acme", None) is None
    assert _email(conn, "k2") is not None


@pytest.mark.parametrize("status,blocked", [
    ("pending", True), ("applied", True), ("interview", True),
    ("discarded", False), ("expired", False),
])
def test_portal_statuses(conn, status, blocked):
    make_job(conn, job_key="p1")
    app_id = repo.create_application(conn, job_key="p1", outreach_type="application_brief",
                                     bucket="eligible", resume_key="fullstack", now=NOW)
    _set(conn, app_id, status)
    assert bool(repo.company_block_reason(conn, "acme", None)) is blocked


def test_portal_blocks_email_at_same_company(conn):
    make_job(conn, job_key="p1")
    repo.create_application(conn, job_key="p1", outreach_type="application_brief",
                            bucket="eligible", resume_key="fullstack", now=NOW)
    assert _email(conn, "e1") is None


def _person(conn):
    conn.execute("INSERT INTO people (company_norm, name, source, verified, fetched_at) "
                 "VALUES ('acme','Sara','yc_page',1,?)", (repo.iso(NOW),))
    return conn.execute("SELECT id FROM people").fetchone()[0]


@pytest.mark.parametrize("status,note,blocked", [
    ("to_connect", "Hi Sara, saw the backend role...", True),
    ("to_connect", None, False),          # plain connect without note is not outreach
    ("requested", None, True),
    ("connected", None, True),
    ("skipped", "Hi Sara", False),
])
def test_linkedin_pitch_blocks_other_channels(conn, status, note, blocked):
    make_job(conn, job_key="n1")
    nid = repo.create_network_entry(conn, job_key="n1", person_id=_person(conn),
                                    connection_note=note, now=NOW)
    conn.execute("UPDATE network SET status=? WHERE id=?", (status, nid))
    assert bool(repo.company_block_reason(conn, "acme", None)) is blocked
    assert (_email(conn, "e1") is None) is blocked


def test_linkedin_requested_marks_contacted(conn):
    make_job(conn, job_key="n1")
    nid = repo.create_network_entry(conn, job_key="n1", person_id=_person(conn),
                                    connection_note="Hi", now=NOW)
    repo.transition_network(conn, nid, "requested", NOW)
    assert repo.is_company_contacted(conn, "acme", None)


def test_domain_identity_matches_across_names(conn):
    _email(conn, "k1", company_norm="acme labs", domain="acme.io")
    # Different display name, same domain -> blocked.
    assert repo.company_block_reason(conn, "acme", "acme.io")
    # Same name, no domain on the new job -> blocked by name fallback.
    assert repo.company_block_reason(conn, "acme labs", None)
    # Unrelated company -> free.
    assert repo.company_block_reason(conn, "globex", "globex.com") is None


def test_contacted_company_stays_blocked_after_discard(conn):
    app_id = _email(conn, "k1")
    _set(conn, app_id, "drafted")
    repo.transition_application(conn, app_id, "sent", NOW)
    _set(conn, app_id, "discarded")  # even if a row is later changed, contact happened
    assert repo.company_block_reason(conn, "acme", None) == "contacted"


def test_two_jobs_same_company_same_run_only_one_passes(conn):
    assert _email(conn, "k1") is not None
    assert _email(conn, "k2") is None


def test_same_job_never_gets_two_applications(conn):
    _set(conn, _email(conn, "k1"), "discarded")
    assert repo.create_application(conn, job_key="k1", outreach_type="direct_email",
                                   bucket="eligible", resume_key="fullstack", now=NOW) is None
