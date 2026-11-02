"""Phase 2 end to end on fixtures: dry run writes files only; live mode uses fake Gmail + Sheets."""
import json
from datetime import timedelta

import pytest

import main
from outputs import sheets_tracker as st
from tests.conftest import NOW
from tests.test_gmail_drafts import FakeGmail
from tests.test_pipeline import FakeClaude, fixture_fetchers
from tests.test_sheets_tracker import FakeSheetsApi


class Gmail(FakeGmail):
    def recent_inbox_headers(self, limit=50):
        return []

    def delete_draft(self, did):
        self.drafts.pop(did, None)


@pytest.fixture
def env(conn, cfg, profile, tmp_path):
    for key in profile["resumes"]:
        pdf = tmp_path / f"{key}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        profile["resumes"][key]["file"] = str(pdf)
    return conn, cfg, profile, tmp_path / "reports"


def run(env, claude, *, now=NOW, live=False, google=None):
    conn, cfg, profile, reports = env
    return main.run_pipeline(conn, cfg, profile, now=now, fetchers=fixture_fetchers(), claude=claude,
                             http=None, report_dir=reports, check_claude=None, live=live, google=google)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_dry_run_writes_files_and_never_touches_applications(env):
    conn = env[0]
    claude = FakeClaude()
    result = run(env, claude)
    assert claude.draft_calls == 1
    out = result["outreach"]
    assert out["drafts_created"] >= 1 and out["kits_created"] >= 1
    assert count(conn, "applications") == 0                    # dry runs never block a company
    rows = conn.execute("SELECT * FROM dry_run_drafts").fetchall()
    assert len(rows) == out["drafts_created"] + out["kits_created"]
    for r in rows:
        text = open(r["file_path"], encoding="utf-8").read()
        assert ("## Body" in text) or ("## Cover note" in text and "Not authorized to work in the US" in text)
    report = result["report"].read_text(encoding="utf-8")
    assert "## Drafts and kits (dry run" in report
    # Drafting prompt only contains shortlisted, live-checked jobs, listing data delimited
    assert "untrusted data" in claude.draft_prompts[0]


def test_dry_run_rerun_makes_no_new_claude_calls(env):
    run(env, FakeClaude())
    again = FakeClaude()
    result = run(env, again, now=NOW + timedelta(hours=2))
    assert again.calls == 0 and again.draft_calls == 0
    assert result["outreach"]["drafts_created"] == 0 and result["outreach"]["existing"]


def test_stretch_jobs_get_email_never_kit(env):
    conn = env[0]
    run(env, FakeClaude())
    for r in conn.execute("SELECT d.outreach_type, j.bucket FROM dry_run_drafts d JOIN jobs j "
                          "ON j.job_key = d.job_key").fetchall():
        if r["bucket"] != "eligible":
            assert r["outreach_type"] == "direct_email"


def test_stretch_email_has_no_pitch_variant_any_more(env):
    conn = env[0]
    run(env, FakeClaude())
    payloads = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT d.payload FROM dry_run_drafts d JOIN jobs j ON j.job_key=d.job_key WHERE j.bucket != 'eligible'")]
    assert payloads and all(p["pitch_variant"] is None for p in payloads)   # a/b test retired


def live_google():
    return {"gmail": Gmail(), "sheets": FakeSheetsApi(), "sheet_id": "sid", "resume_links": {}, "notes": []}


def test_live_creates_gmail_drafts_and_sheet_rows_once(env):
    conn = env[0]
    google = live_google()
    result = run(env, FakeClaude(), live=True, google=google)
    out = result["outreach"]
    assert len(google["gmail"].drafts) == out["drafts_created"] >= 1
    apps = conn.execute("SELECT * FROM applications").fetchall()
    assert {a["outreach_type"] for a in apps} == {"direct_email", "application_brief"}
    assert len(google["sheets"].tabs[st.APPS]) == 1 + out["kits_created"]
    assert len(google["sheets"].tabs[st.KITS]) == 1 + out["kits_created"]
    # recipient comes from the parsed listing, never from Claude
    for a in apps:
        if a["outreach_type"] == "direct_email":
            job = conn.execute("SELECT contact_email FROM jobs WHERE job_key=?", (a["job_key"],)).fetchone()
            assert a["recipient"] == job["contact_email"]
    drafts_before = len(google["gmail"].drafts)
    again = FakeClaude()
    run(env, again, now=NOW + timedelta(hours=1), live=True, google=google)
    assert again.draft_calls == 0 and len(google["gmail"].drafts) == drafts_before
    assert len(google["sheets"].tabs[st.APPS]) == 1 + out["kits_created"]


def test_live_gmail_error_then_recovered(env):
    conn = env[0]
    google = live_google()
    google["gmail"].fail_next = 99
    run(env, FakeClaude(), live=True, google=google)
    failed = conn.execute("SELECT COUNT(*) FROM applications WHERE status='draft_failed'").fetchone()[0]
    assert failed >= 1 and not google["gmail"].drafts
    google["gmail"].fail_next = 0
    run(env, FakeClaude(), now=NOW + timedelta(hours=1), live=True, google=google)
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE status='draft_failed'").fetchone()[0] == 0
    assert len(google["gmail"].drafts) == failed


def test_live_sheet_down_rows_queued_then_written(env):
    conn = env[0]
    google = live_google()
    google["sheets"].down = True
    result = run(env, FakeClaude(), live=True, google=google)
    assert any("sheet write failed" in e for e in result["errors"])
    queued = conn.execute("SELECT COUNT(*) FROM sheet_queue WHERE written_at IS NULL").fetchone()[0]
    assert queued >= 2
    google["sheets"].down = False
    run(env, FakeClaude(), now=NOW + timedelta(hours=1), live=True, google=google)
    assert conn.execute("SELECT COUNT(*) FROM sheet_queue WHERE written_at IS NULL").fetchone()[0] == 0


def test_daily_caps_respected(env):
    conn, cfg, profile, reports = env
    cfg["caps"]["email_drafts"] = 1
    cfg["caps"]["portal_kits"] = 1
    result = run(env, FakeClaude())
    assert result["outreach"]["drafts_created"] <= 1 and result["outreach"]["kits_created"] <= 1


def test_drafting_failure_marks_run_failed(env):
    from pipeline.claude_cli import ClaudeError
    from schemas.ai_output import DraftBatch

    class DraftDown(FakeClaude):
        def __call__(self, prompt, model, schema, retries, timeout, **kw):
            if schema is DraftBatch:
                raise ClaudeError("draft call failed")
            return super().__call__(prompt, model, schema, retries, timeout, **kw)
    result = run(env, DraftDown())
    assert result["status"] == "failed" and any(e.startswith("drafting failed") for e in result["errors"])


# ---------------------------------------------------------------- Network tab (Phase 3)

def stretch_with_founder_link(conn, cfg, profile):
    """A US-auth stretch job with only a portal link and a founder LinkedIn link in the listing."""
    from fetchers.models import RawJob
    raw = RawJob(source="hn", external_id="5555", url="https://news.ycombinator.com/item?id=5555",
                 company="Tinyco", title="Backend Engineer", location_text="Tinyco | Backend Engineer | Remote (US)",
                 description="Tinyco | Backend Engineer | Remote (US)\nMust be authorized to work in the US. "
                             "Team of 6. Django and Postgres.\nApply: https://jobs.ashbyhq.com/tinyco/"
                             "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                 links=["https://jobs.ashbyhq.com/tinyco/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "https://www.linkedin.com/in/sam-tinyco"],
                 posted_at=NOW - timedelta(days=1))
    return main.process_raw(conn, raw, cfg, profile, NOW, None)


def test_stretch_portal_with_founder_link_becomes_linkedin_pitch(env):
    conn, cfg, profile, reports = env
    key = stretch_with_founder_link(conn, cfg, profile)
    row = conn.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    assert (row["eligibility"], row["bucket"], row["route"]) == ("stretch", "us_auth_stretch", "network")
    google = live_google()
    claude = FakeClaude()
    result = run(env, claude, live=True, google=google)
    assert result["outreach"]["pitches_created"] == 1
    assert "network: pitch" in claude.draft_prompts[0]
    net = conn.execute("SELECT * FROM network WHERE job_key=?", (key,)).fetchone()
    assert net["status"] == "to_connect" and net["is_pitch"] == 1 and net["connection_note"] is None
    assert "Pakistan" in net["after_accept_message"] and "{first_name}" not in net["after_accept_message"]
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE job_key=?", (key,)).fetchone()[0] == 0  # no portal kit
    sheet_row = next(r for r in google["sheets"].tabs[st.NETWORK] if r[0] == net["id"])
    assert sheet_row[st.NET_COL["Verified"]] == "Check first" and sheet_row[st.NET_COL["Status"]] == "To connect"
    assert sheet_row[st.NET_COL["Source"]] == "Job listing" and "Open profile" in sheet_row[st.NET_COL["LinkedIn"]]


def test_network_status_sync_and_contact(env):
    conn, cfg, profile, reports = env
    key = stretch_with_founder_link(conn, cfg, profile)
    google = live_google()
    run(env, FakeClaude(), live=True, google=google)
    net = conn.execute("SELECT * FROM network WHERE job_key=?", (key,)).fetchone()
    sheet_row = next(r for r in google["sheets"].tabs[st.NETWORK] if r[0] == net["id"])
    sheet_row[st.NET_COL["Status"]] = "connected"            # I jump straight to connected
    sheet_row[st.NET_COL["Notes"]] = "accepted fast"
    out = st.sync_network(conn, google["sheets"], "sid", NOW)
    assert out["connected"] == 1
    net = conn.execute("SELECT * FROM network WHERE job_key=?", (key,)).fetchone()
    assert (net["status"], net["notes"]) == ("connected", "accepted fast")
    from db import repo
    assert repo.is_company_contacted(conn, "tinyco", None)   # note was sent -> one outreach per company


def test_pitches_need_no_note_budget(env):
    conn, cfg, profile, reports = env
    cfg.pop("linkedin_notes_per_month", None)               # no such limit any more
    stretch_with_founder_link(conn, cfg, profile)
    result = run(env, FakeClaude(), live=True, google=live_google())
    assert result["outreach"]["pitches_created"] == 1


def test_emailed_jobs_get_no_connection_note(env):
    conn, cfg, profile, reports = env
    from pipeline import people as people_mod
    # Give Rocketship (emailed stretch job in the fixtures) a known founder.
    people_mod.save_people(conn, "rocketship", [{"name": "Sam Founder", "title": "CEO",
                                                 "linkedin_url": "https://linkedin.com/in/sam", "source": "team_page",
                                                 "verified": 1}], NOW)
    run(env, FakeClaude(), live=True, google=live_google())
    rows = conn.execute("SELECT n.* FROM network n JOIN jobs j ON j.job_key=n.job_key "
                        "WHERE j.company_norm='rocketship'").fetchall()
    assert rows and all(r["connection_note"] is None and r["after_accept_message"] and not r["is_pitch"]
                        for r in rows)


def test_each_person_gets_their_own_name():
    from pipeline.outreach import personalize
    people = [{"name": "Berat Celik"}, {"name": "Eren Berke Saglam"}, {"name": "(profile x)"}]
    assert personalize("Thanks for connecting, {first_name}!", people[1], people) == "Thanks for connecting, Eren!"
    assert personalize("Thanks for connecting, Berat!", people[1], people) == "Thanks for connecting, Eren!"
    assert personalize("Hi {first_name}", people[2], people) == "Hi there"
    assert personalize(None, people[0], people) is None
