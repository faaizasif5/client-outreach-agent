"""Gmail drafts (spec section 14) against a fake Gmail client: no network, no Google APIs."""
import email
import json

import pytest

from db import repo
from outputs import drafts
from outputs.gmail_client import JOB_HEADER, build_mime
from schemas.ai_output import Draft
from tests.conftest import NOW, make_job


class FakeGmail:
    address = "me@example.com"

    def __init__(self):
        self.drafts, self.threads, self.fail_next = {}, {}, 0
        self.n = 0

    def create_draft(self, msg):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("Gmail 500")
        self.n += 1
        did, tid = f"d{self.n}", f"t{self.n}"
        self.drafts[did] = msg
        self.threads[tid] = {"sent": False, "sent_at_ms": None, "reply": False}
        return did, tid

    def draft_exists(self, did):
        return did in self.drafts

    def find_draft(self, job_key):
        for did, msg in self.drafts.items():
            if msg[JOB_HEADER] == job_key:
                return did, f"t{did[1:]}"
        return None

    def thread_status(self, tid):
        return self.threads[tid]

    # test helpers
    def user_sends(self, did):
        tid = f"t{did[1:]}"
        del self.drafts[did]
        self.threads[tid] = {"sent": True, "sent_at_ms": 1_792_000_000_000, "reply": False}


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "ALEX_EXAMPLE_RESUME_FULLSTACK.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    return p


DRAFT = Draft(job_key="k1", outreach_type="direct_email", subject="Backend role", body="Hi\nShort note.")


def _create(conn, gmail, cfg, pdf, key="k1", company="acme"):
    make_job(conn, job_key=key, company_norm=company, contact_email="jobs@acme.com", bucket="eligible")
    job = dict(repo.get_job(conn, key))
    return drafts.create_email_draft(conn, gmail, job=job, draft=DRAFT, resume_key="fullstack",
                                     resume_path=str(pdf), pitch_variant=None, cfg=cfg, now=NOW)


def app(conn, key="k1"):
    return conn.execute("SELECT * FROM applications WHERE job_key=?", (key,)).fetchone()


def test_mime_has_body_attachment_and_job_header(pdf):
    msg = build_mime(sender="me@example.com", to="jobs@acme.com", subject="Hi", body="Body text",
                     job_key="k1", attachment=pdf, filename="Alex_Example_Resume.pdf")
    parsed = email.message_from_bytes(msg.as_bytes())
    assert parsed.get_content_type() == "multipart/mixed"
    parts = list(parsed.walk())
    att = [p for p in parts if p.get_filename()]
    assert att[0].get_filename() == "Alex_Example_Resume.pdf" and att[0].get_content_type() == "application/pdf"
    assert parsed[JOB_HEADER] == "k1" and parsed["To"] == "jobs@acme.com"


def test_create_draft_happy_path(conn, cfg, pdf):
    gmail = FakeGmail()
    assert _create(conn, gmail, cfg, pdf) == "drafted"
    a = app(conn)
    assert (a["status"], a["draft_id"], a["recipient"]) == ("drafted", "d1", "jobs@acme.com")
    assert json.loads(a["draft_json"])["subject"] == "Backend role"
    # Recipient comes from the parsed listing contact, attachment uses the configured name
    msg = gmail.drafts["d1"]
    assert msg["To"] == "jobs@acme.com"
    assert [p.get_filename() for p in msg.iter_attachments()] == [cfg["outreach"]["attachment_filename"]]


def test_rerun_never_duplicates(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf)
    job = dict(repo.get_job(conn, "k1"))
    again = drafts.create_email_draft(conn, gmail, job=job, draft=DRAFT, resume_key="fullstack",
                                      resume_path=str(pdf), pitch_variant=None, cfg=cfg, now=NOW)
    assert again == "exists" and len(gmail.drafts) == 1


def test_second_company_job_suppressed(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf, key="k1")
    assert _create(conn, gmail, cfg, pdf, key="k2") == "suppressed"
    assert len(gmail.drafts) == 1


def test_gmail_error_then_recovered_next_run(conn, cfg, pdf):
    gmail = FakeGmail()
    gmail.fail_next = 1
    assert _create(conn, gmail, cfg, pdf) == "draft_failed"
    assert "Gmail 500" in app(conn)["last_error"]
    assert drafts.recover(conn, gmail, cfg, NOW)["retried"] == 1
    assert app(conn)["status"] == "drafted" and len(gmail.drafts) == 1


def test_missing_resume_fails_cleanly(conn, cfg, tmp_path):
    gmail = FakeGmail()
    assert _create(conn, gmail, cfg, tmp_path / "nope.pdf") == "draft_failed"
    assert "resume PDF missing" in app(conn)["last_error"] and not gmail.drafts


def test_crash_between_steps_finds_existing_draft(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf)
    conn.execute("UPDATE applications SET status='drafting', draft_id=NULL WHERE job_key='k1'")  # simulate crash
    out = drafts.recover(conn, gmail, cfg, NOW)
    assert out["recovered"] == 1 and len(gmail.drafts) == 1 and app(conn)["draft_id"] == "d1"


def test_reconcile_sent_and_discarded(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf, key="k1", company="acme")
    _create(conn, gmail, cfg, pdf, key="k2", company="globex")
    gmail.user_sends("d1")
    del gmail.drafts["d2"]                       # deleted without sending
    out = drafts.reconcile(conn, gmail, NOW)
    assert out == {"sent": 1, "discarded": 1, "replied": 0}
    assert app(conn, "k1")["status"] == "sent" and app(conn, "k1")["sent_at"]
    assert repo.is_company_contacted(conn, "acme", None)
    assert app(conn, "k2")["status"] == "discarded"
    assert not repo.is_company_contacted(conn, "globex", None)
    assert repo.company_block_reason(conn, "globex", None) is None   # free again


def test_reply_detected(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf)
    gmail.user_sends("d1")
    drafts.reconcile(conn, gmail, NOW)
    gmail.threads["t1"]["reply"] = True
    assert drafts.reconcile(conn, gmail, NOW)["replied"] == 1
    assert app(conn)["status"] == "replied"


def test_unsent_draft_left_alone(conn, cfg, pdf):
    gmail = FakeGmail()
    _create(conn, gmail, cfg, pdf)
    assert drafts.reconcile(conn, gmail, NOW) == {"sent": 0, "discarded": 0, "replied": 0}
    assert app(conn)["status"] == "drafted"
