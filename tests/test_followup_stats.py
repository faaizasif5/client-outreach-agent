"""Freshness check #3, no_response, and weekly stats (Phase 3)."""
from datetime import timedelta

from db import repo
from outputs import drafts, stats
from tests.conftest import NOW, make_job
from tests.test_gmail_drafts import FakeGmail


class Gmail(FakeGmail):
    def delete_draft(self, did):
        self.drafts.pop(did, None)


def drafted(conn, key, company, created, gmail):
    make_job(conn, job_key=key, company_norm=company, contact_email=f"jobs@{company}.com")
    app_id = repo.create_application(conn, job_key=key, outreach_type="direct_email", bucket="eligible",
                                     resume_key="fullstack", now=created)
    did, tid = gmail.create_draft({"X-Job-Agent-Key": key})
    repo.transition_application(conn, app_id, "drafted", created, draft_id=did, thread_id=tid)
    return app_id


def status(conn, key):
    return conn.execute("SELECT status FROM applications WHERE job_key=?", (key,)).fetchone()[0]


def test_stale_drafts(conn, cfg):
    gmail = Gmail()
    drafted(conn, "fresh", "a", NOW - timedelta(days=1), gmail)
    drafted(conn, "mid_open", "b", NOW - timedelta(days=4), gmail)
    drafted(conn, "mid_closed", "c", NOW - timedelta(days=4), gmail)
    drafted(conn, "old", "d", NOW - timedelta(days=8), gmail)
    checked = []

    def liveness(job):
        checked.append(job["job_key"])
        return "closed" if job["job_key"] == "mid_closed" else "open"
    out = drafts.cleanup_stale(conn, gmail, cfg, NOW, liveness=liveness)
    assert out == {"expired": 2, "checked": 2}
    assert sorted(checked) == ["mid_closed", "mid_open"]           # fresh drafts are not re-checked
    assert [status(conn, k) for k in ("fresh", "mid_open", "mid_closed", "old")] == \
        ["drafted", "drafted", "expired", "expired"]
    assert len(gmail.drafts) == 2                                   # their drafts were deleted
    assert repo.company_block_reason(conn, "d", None) is None       # expired frees the company


def test_no_response_after_21_days(conn):
    gmail = Gmail()
    for key, days in (("old", 22), ("recent", 10)):
        app_id = drafted(conn, key, key, NOW - timedelta(days=days + 1), gmail)
        repo.transition_application(conn, app_id, "sent", NOW - timedelta(days=days))
    assert drafts.mark_no_response(conn, NOW, 21) == 1
    assert status(conn, "old") == "no_response" and status(conn, "recent") == "sent"
    assert repo.is_company_contacted(conn, "old", None)            # still never contacted twice


def test_weekly_stats(conn, cfg):
    gmail = Gmail()
    for n, (bucket, variant, final) in enumerate([
            ("eligible", None, "replied"), ("eligible", None, "sent"),
            ("us_auth_stretch", "a", "sent"), ("us_auth_stretch", "b", "replied")]):
        key = f"k{n}"
        make_job(conn, job_key=key, company_norm=f"c{n}", title="Forward Deployed Engineer" if n == 0 else "Backend")
        app_id = repo.create_application(conn, job_key=key, outreach_type="direct_email", bucket=bucket,
                                         resume_key="fullstack", now=NOW - timedelta(days=3), pitch_variant=variant)
        repo.transition_application(conn, app_id, "drafted", NOW - timedelta(days=3), draft_id="d", thread_id="t")
        repo.transition_application(conn, app_id, "sent", NOW - timedelta(days=2))
        if final == "replied":
            repo.transition_application(conn, app_id, "replied", NOW - timedelta(days=1))
    lines = stats.weekly(conn, NOW, cfg)
    text = "\n".join(lines)
    assert "4 emails sent" in text
    assert "| bucket | us_auth_stretch | 2 | 1 | 50% | 0 |" in text
    assert "| pitch variant | a | 1 | 0 | 0% | 0 |" in text and "| pitch variant | b | 1 | 1 | 100% | 0 |" in text
    assert "| role type | fde | 1 | 1 | 100% | 0 |" in text


def test_stretch_bucket_flagged_not_disabled(conn, cfg):
    cfg["stats"]["stretch_flag_sent"] = 3
    for n in range(3):
        key = f"s{n}"
        make_job(conn, job_key=key, company_norm=f"s{n}")
        app_id = repo.create_application(conn, job_key=key, outreach_type="direct_email", bucket="remote_us_stretch",
                                         resume_key="fullstack", now=NOW - timedelta(days=30))
        repo.transition_application(conn, app_id, "drafted", NOW, draft_id="d", thread_id="t")
        repo.transition_application(conn, app_id, "sent", NOW)
    text = "\n".join(stats.weekly(conn, NOW, cfg))
    assert "**remote_us_stretch**: 3 sent with no positive reply" in text


def test_stats_day_is_monday_karachi(cfg):
    monday = NOW + timedelta(days=(7 - NOW.weekday()) % 7)   # NOW is a Saturday
    assert stats.is_stats_day(monday.replace(hour=3), cfg)
    assert not stats.is_stats_day(NOW, cfg)
