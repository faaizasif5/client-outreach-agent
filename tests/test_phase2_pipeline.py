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


def test_stretch_email_has_pitch_variant(env):
    conn = env[0]
    run(env, FakeClaude())
    payloads = [json.loads(r["payload"]) for r in conn.execute(
        "SELECT d.payload FROM dry_run_drafts d JOIN jobs j ON j.job_key=d.job_key WHERE j.bucket != 'eligible'")]
    assert payloads and all(p["pitch_variant"] in ("a", "b") for p in payloads)


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
