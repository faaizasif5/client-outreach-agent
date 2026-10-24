"""Integration: fixtures -> full dry-run pipeline (mocked Claude) -> report; then a second run."""
import json
import re
from datetime import timedelta

import pytest

import main
from fetchers import hn_whos_hiring, remoteok, wwr
from schemas.ai_output import DraftBatch, ScoreBatch
from tests.conftest import FIXTURES, NOW


def fixture_fetchers():
    thread = json.loads((FIXTURES / "hn_thread.json").read_text(encoding="utf-8"))
    ro = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    rss = (FIXTURES / "wwr.rss").read_text(encoding="utf-8")
    return {"hn": lambda: hn_whos_hiring.parse_thread(thread),
            "remoteok": lambda: remoteok.parse(ro), "wwr": lambda: wwr.parse(rss)}


EMAIL_BODY = ("Hi,\nI cut API latency by 30% with Redis caching on a client project.\n"
              "I work as a B2B contractor with 4 hours of US overlap.\nOpen to a short call?\nAlex Example")
STRETCH_BODY = ("Hi,\nI cut API latency by 30% with Redis caching on a client project.\n"
                "I know the role asks for US-based candidates; I'm based in Pakistan.\n"
                "I work as a B2B contractor, so no payroll setup, with US-hours overlap.\n"
                "Would a short call make sense?\nAlex Example")


class FakeClaude:
    """Answers scoring and drafting calls for every job_key in the prompt; never the real CLI."""
    def __init__(self, fit=85):
        self.calls, self.prompts, self.fit = 0, [], fit
        self.draft_calls, self.draft_prompts = 0, []
        self.tailor_summary = "Full-stack engineer with 3+ years building Vue.js and Django apps."

    def __call__(self, prompt, model, schema, retries, timeout, **kw):
        self.kwargs = kw
        usage = {"model": model, "effort": kw.get("effort"), "attempts": 1,
                 "calls": [{"usage": {"input_tokens": len(prompt) // 4, "output_tokens": 500}, "duration_ms": 1000}]}
        if schema is DraftBatch:
            self.draft_calls += 1
            self.draft_prompts.append(prompt)
            results = []
            for block in re.findall(r'<listing job_key="[^"]+">.*?</listing>', prompt, re.S):
                key = re.search(r'job_key="([^"]+)"', block).group(1)
                otype = re.search(r"outreach_type: (\w+)", block).group(1)
                bucket = re.search(r"bucket: (\w+)", block).group(1)
                mode = re.search(r"network: (\w+)", block).group(1)
                mention_line = re.search(r"mention: (.+)", block)
                mention = [] if not mention_line or mention_line.group(1) == "none" else mention_line.group(1).split(", ")
                stack_bit = f" I work with {', '.join(mention[:2])}." if mention else ""
                tailored = None
                if "tailor: yes" in block:
                    rkey = re.search(r"resume: (\w+)", block).group(1)
                    ids = re.findall(r"^  ([\w-]+) \[", prompt, re.M)
                    tailored = {"resume_key": rkey, "summary": self.tailor_summary,
                                "bullet_ids": ids[::-1][:3], "skills_first": ["Django"]}
                network = None if mode == "none" else {
                    "connection_note": None,
                    "after_accept_message": ("Thanks for connecting, {first_name}." + stack_bit +
                                             " I move across the stack with agentic coding in Claude Code. "
                                             "I know you're hiring in the US and I'm in Pakistan; up for a quick call?")
                    if mode == "pitch" else ("Thanks for connecting, {first_name}!" + stack_bit)}
                if otype == "network_pitch":
                    results.append({"job_key": key, "outreach_type": otype, "subject": None, "body": "",
                                    "network": network})
                elif otype == "direct_email":
                    results.append({"job_key": key, "outreach_type": otype, "subject": "Backend role",
                                    "body": (EMAIL_BODY if bucket == "eligible" else STRETCH_BODY) + stack_bit,
                                    "network": network, "tailored": tailored})
                else:
                    results.append({"job_key": key, "outreach_type": otype, "subject": None, "body": "",
                                    "kit": {"cover_note": "I build Django APIs." + stack_bit, "form_answers": [
                                        {"question": "Why us?", "answer": "The product fits my Django work."}]},
                                    "network": network, "tailored": tailored})
            return schema.model_validate({"results": results}), usage
        self.calls += 1
        self.prompts.append(prompt)
        keys = re.findall(r'<listing job_key="([^"]+)">', prompt)
        results = [{"job_key": k, "resume": "fde" if "Forward" in prompt.split(k)[1][:200] else "fullstack",
                    "fit_score": self.fit, "evidence": ["Listing: 'Django'", "Profile: Django in production"],
                    "reason": "Backend-heavy role", "confidence": "high", "skip": False,
                    "skip_reasons": []} for k in keys]
        return schema.model_validate({"results": results}), usage


@pytest.fixture
def setup(conn, cfg, profile, tmp_path):
    pdf = tmp_path / "fullstack.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    for key in profile["resumes"]:
        profile["resumes"][key]["file"] = str(tmp_path / f"{key}.pdf")
    return conn, cfg, profile, tmp_path / "reports"


def run(setup, claude, now=NOW):
    conn, cfg, profile, reports = setup
    return main.run_pipeline(conn, cfg, profile, now=now, fetchers=fixture_fetchers(), claude=claude,
                             http=None, report_dir=reports, check_claude=None)


def job(conn, key):
    return conn.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()


ACME_ASHBY = "ats:ashby:11111111-2222-3333-4444-555555555555"


def test_full_run(setup):
    conn = setup[0]
    claude = FakeClaude()
    result = run(setup, claude)
    assert result["status"] == "success" and claude.calls == 1

    # Same Acme role on HN + RemoteOK -> one row with two sources
    acme = job(conn, ACME_ASHBY)
    assert {s["source"] for s in json.loads(acme["sources"])} == {"hn", "remoteok"}
    assert acme["eligibility"] == "eligible" and acme["route"] == "portal"

    expect = {
        "src:hn:9002": ("stretch", "remote_us_stretch"),     # Rocketship, obfuscated founder email
        "src:hn:9003": ("ineligible", "stale"),              # Oct 1 comment, 23 days old
        "src:hn:9004": ("ineligible", "onsite"),
        "src:hn:9005": ("eligible", "worldwide"),
        "src:hn:9006": ("ineligible", "closed"),             # author replied "filled"
        "src:hn:9007": ("ineligible", "region_locked"),
        "src:hn:9008": ("uncertain", "region_unknown"),
        "src:hn:9009": ("ineligible", "regulated"),
        "src:remoteok:503": ("uncertain", "region_unknown"),
        "src:remoteok:504": ("ineligible", "stale"),
        "src:remoteok:505": ("eligible", "worldwide"),      # undated; "Posted 2 days ago"
        "src:wwr:statesider-backend-developer": ("ineligible", "stretch_no_direct_contact"),
        "src:wwr:mapster-senior-net-engineer": ("uncertain", "conflicting_signals"),
    }
    for key, (elig, reason) in expect.items():
        r = job(conn, key)
        got = (r["eligibility"], r["bucket"] if r["eligibility"] == "stretch" else r["eligibility_reason"])
        assert got == (elig, reason), key
    assert job(conn, "src:hn:9002")["contact_email"] == "sam@rocketship.dev"
    assert job(conn, "src:hn:9002")["route"] == "email"
    assert job(conn, "src:remoteok:505")["posted_at_source"] == "relative_text"
    assert job(conn, "src:remoteok:502")["status"] == "keyword_filtered"
    assert "travel_occasional" in json.loads(job(conn, "src:hn:9005")["flags"])

    # Only eligible/stretch, fresh, open jobs went to Claude
    prompt = claude.prompts[0]
    for bad in ("src:hn:9003", "src:hn:9004", "src:hn:9006", "src:hn:9008", "src:remoteok:502"):
        assert f'job_key="{bad}"' not in prompt
    assert "<listing" in prompt and "untrusted data" in prompt

    # Injection text has no effect on the recipient
    assert job(conn, "src:hn:9012")["contact_email"] == "jobs@injecto.io"

    report = result["report"].read_text(encoding="utf-8")
    assert "## Shortlist" in report and "| label |" in report
    assert ACME_ASHBY in report
    # Second Acme role (same company) is suppressed in the same run
    acme_fe = job(conn, "ats:ashby:66666666-7777-8888-9999-000000000000")
    assert acme_fe["status"] == "suppressed_company"
    assert "Second role at a blocked company" in report and "Frontend Engineer (Vue)" in report


def test_no_ineligible_or_stale_job_in_shortlist(setup):
    conn = setup[0]
    run(setup, FakeClaude())
    from pipeline import scorer
    for item in scorer.shortlist(conn, setup[1], setup[2], NOW):
        if item["decision"] in ("shortlisted", "queued"):
            assert item["eligibility"] in ("eligible", "stretch")
            assert NOW - main.repo.parse_iso(item["posted_at"]) <= timedelta(days=14)


def test_second_run_creates_no_duplicates_and_no_claude_call(setup):
    conn = setup[0]
    first = FakeClaude()
    run(setup, first)
    jobs1 = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    evals1 = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    report1 = run(setup, FakeClaude(), now=NOW + timedelta(hours=2))["report"]
    second = FakeClaude()
    result = run(setup, second, now=NOW + timedelta(hours=3))
    assert second.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs1
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == evals1
    sources = json.loads(job(conn, ACME_ASHBY)["sources"])
    assert len(sources) == 2
    assert result["report"] == report1


def test_labels_survive_rerun_and_feed_calibration(setup):
    conn = setup[0]
    report = run(setup, FakeClaude())["report"]
    text = report.read_text(encoding="utf-8")
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if line.startswith("| 1 |"):
            lines[n] = line[: line.rstrip().rstrip("|").rstrip().rfind("|")] + "| would_apply |"
            break
    report.write_text("\n".join(lines), encoding="utf-8")
    run(setup, FakeClaude(), now=NOW + timedelta(hours=1))           # regenerates today's report
    assert "would_apply" in report.read_text(encoding="utf-8")    # label kept
    out = main.label_command(conn, [report])
    assert "Labeled jobs: 1" in out
    assert conn.execute("SELECT label FROM labels").fetchone()[0] == "would_apply"


def test_claude_failure_marks_run_failed_and_writes_report(setup):
    from pipeline.claude_cli import ClaudeError

    def broken(*a, **k):
        err = ClaudeError("failed after retries: bad json")
        err.usage = {"attempts": 3}
        raise err
    result = run(setup, broken)
    assert result["status"] == "failed"
    assert any("scoring failed" in e for e in result["errors"])
    assert result["report"].exists()


def test_source_failure_does_not_stop_others(setup):
    conn, cfg, profile, reports = setup
    fetchers = fixture_fetchers()
    fetchers["hn"] = lambda: (_ for _ in ()).throw(RuntimeError("algolia down"))
    result = main.run_pipeline(conn, cfg, profile, now=NOW, fetchers=fetchers, claude=FakeClaude(),
                               http=None, report_dir=reports, check_claude=None)
    assert any("algolia down" in e for e in result["errors"])
    assert result["counts"]["fetched_remoteok"] == 5


def test_explicit_us_only_toggle_end_to_end(setup):
    conn, cfg, profile, reports = setup
    cfg["pitch_explicit_us_only"] = False
    from fetchers.models import RawJob
    raw = RawJob(source="hn", external_id="77", url="https://news.ycombinator.com/item?id=77",
                 company="Tiny", title="Backend Engineer", location_text="Tiny | Backend Engineer | Remote",
                 description="Tiny | Backend Engineer | Remote\nUS-based only. Please do not apply from "
                             "outside the US. Team of 4. Email jobs@tiny.dev",
                 posted_at=NOW - timedelta(days=1))
    main.process_raw(conn, raw, cfg, profile, NOW, None)
    r = job(conn, "src:hn:77")
    assert (r["eligibility"], r["eligibility_reason"]) == ("ineligible", "explicit_us_only_disabled")


def test_rescore_adds_new_evaluations_and_uses_latest(setup):
    conn, cfg, profile, reports = setup
    run(setup, FakeClaude(fit=85))
    n = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
    again = FakeClaude(fit=65)
    main.run_pipeline(conn, cfg, profile, now=NOW + timedelta(hours=1), fetchers=fixture_fetchers(),
                      claude=again, http=None, report_dir=reports, check_claude=None, rescore=True)
    assert again.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 2 * n
    from pipeline import scorer
    assert {i["fit_score"] for i in scorer.shortlist(conn, cfg, profile, NOW)} == {65}
    assert again.kwargs["effort"] == cfg["scoring"]["effort"]


def test_keyword_added_later_brings_filtered_jobs_back(setup):
    conn, cfg, profile, reports = setup
    from fetchers.models import RawJob
    from datetime import timedelta as td
    raw = RawJob(source="hn", external_id="4242", url="https://news.ycombinator.com/item?id=4242",
                 company="Stoa", title="Member of Technical Staff", location_text="Stoa | MTS | Remote (Worldwide)",
                 description="Remote (Worldwide). Django and Postgres. Email jobs@stoa.dev",
                 posted_at=NOW - td(days=1))
    cfg["keywords"]["role_title_include"] = [k for k in cfg["keywords"]["role_title_include"]
                                             if "technical staff" not in k and "mts" not in k]
    main.process_raw(conn, raw, cfg, profile, NOW, None)
    assert main.scoring_candidates(conn, cfg, profile, NOW) == []
    assert job(conn, "src:hn:4242")["status"] == "keyword_filtered"
    cfg["keywords"]["role_title_include"].append("member of technical staff")
    assert [j["job_key"] for j in main.scoring_candidates(conn, cfg, profile, NOW)] == ["src:hn:4242"]
