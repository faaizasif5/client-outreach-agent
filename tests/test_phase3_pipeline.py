"""Phase 3 end to end on fixtures: tailored resumes in the drafting stage, YC as a source."""
import json
import re
import shutil

import pytest

import main
from fetchers import yc_scraper
from pipeline import resume_builder as rb
from tests.conftest import FIXTURES, NOW, ROOT
from tests.test_phase2_pipeline import env, live_google, run  # noqa: F401  (fixtures/helpers)
from tests.test_pipeline import FakeClaude, fixture_fetchers


@pytest.fixture
def templates(tmp_path, profile, monkeypatch):
    d = tmp_path / "latex"
    d.mkdir()
    shutil.copy(FIXTURES / "resume_fixture.tex", d / "fullstack.tex")
    monkeypatch.setattr(rb, "LATEX_DIR", d)
    monkeypatch.setattr(rb, "BULLETS", tmp_path / "bullets.yaml")
    rb.build_templates(profile=profile)
    return d


def fake_compiler(monkeypatch, pages=1):
    monkeypatch.setattr(rb, "compiler_path", lambda cfg: "fake")

    def compile_fn(tex, out_dir, compiler, filename, timeout=300):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "resume.tex").write_text(tex, encoding="utf-8")
        pdf = out_dir / filename
        pdf.write_bytes(b"%PDF " + b"/Type /Page\n" * pages)
        return pdf, ""
    monkeypatch.setattr(rb, "compile_tex", compile_fn)


def test_top_matches_get_tailored_resume_attached(env, templates, monkeypatch):
    conn = env[0]
    fake_compiler(monkeypatch)
    google = live_google()
    claude = FakeClaude(fit=85)
    result = run(env, claude, live=True, google=google)
    prompt = claude.draft_prompts[0]
    assert re.search(r"^tailor: yes$", prompt, re.M) and "BULLET BANK" in prompt
    rows = conn.execute("SELECT * FROM tailored_resumes").fetchall()
    assert rows and all(r["status"] == "used" for r in rows), [r["problems"] for r in rows]
    for r in rows:
        assert r["pdf_path"].endswith("Resume.pdf")
        spec = json.loads(r["spec_json"])
        assert spec["resume_key"] == "fullstack" and spec["skills_first"] == ["Django"]
    # the email draft carries the tailored PDF, not the standard one
    tailored_paths = {r["job_key"]: r["pdf_path"] for r in rows}
    for app in conn.execute("SELECT * FROM applications WHERE outreach_type='direct_email'").fetchall():
        if app["job_key"] in tailored_paths:
            assert app["resume_pdf_path"] == tailored_paths[app["job_key"]]
    assert result["outreach"]["tailored"]


def test_invented_summary_rejected_standard_pdf_used(env, templates, monkeypatch):
    conn = env[0]
    fake_compiler(monkeypatch)
    claude = FakeClaude(fit=85)
    claude.tailor_summary = "Engineer who scaled Google search to 900 million users."
    run(env, claude, live=True, google=live_google())
    rows = conn.execute("SELECT * FROM tailored_resumes").fetchall()
    assert rows and all(r["status"] == "rejected" for r in rows)
    assert any("not in profile" in p or "not in the profile" in p for r in rows for p in json.loads(r["problems"]))
    standard = {a["resume_pdf_path"] for a in conn.execute("SELECT resume_pdf_path FROM applications")}
    assert not any("Resume.pdf" in (p or "") for p in standard)


def test_two_page_tailored_resume_rejected(env, templates, monkeypatch):
    fake_compiler(monkeypatch, pages=2)
    run(env, FakeClaude(fit=85), live=True, google=live_google())
    rows = env[0].execute("SELECT problems FROM tailored_resumes").fetchall()
    assert rows and all("2 pages" in r["problems"] for r in rows)


def test_below_80_not_tailored(env, templates, monkeypatch):
    fake_compiler(monkeypatch)
    claude = FakeClaude(fit=75)
    run(env, claude, live=True, google=live_google())
    assert not re.search(r"^tailor: yes$", claude.draft_prompts[0], re.M)
    assert env[0].execute("SELECT COUNT(*) FROM tailored_resumes").fetchone()[0] == 0


def test_yc_jobs_flow_through_gate_and_founders_route_to_network(env, tmp_path, monkeypatch):
    conn, cfg, profile, reports = env
    monkeypatch.delenv("YC_ENABLED", raising=False)
    sel_path = tmp_path / "yc_selectors.yaml"
    shutil.copy(ROOT / "config" / "yc_selectors.yaml", sel_path)
    from tests.test_yc_scraper import Http, pages
    info_holder = {}

    def yc():
        def no_claude(*a, **k):
            raise AssertionError("the real Claude CLI must never be called in tests")
        raws, info = yc_scraper.fetch(conn, Http(), NOW, cfg=cfg, load_page=pages(), claude=no_claude,
                                      selectors_path=sel_path)
        info_holder["yc"] = info
        return raws
    result = main.run_pipeline(conn, cfg, profile, now=NOW, fetchers={"yc": yc}, claude=FakeClaude(),
                               http=None, report_dir=reports, check_claude=None, source_info=info_holder)
    assert result["counts"]["fetched_yc"] == 14
    assert info_holder["yc"]["status"] == "healthy"
    blue = conn.execute("SELECT * FROM jobs WHERE company_norm='bluecargo'").fetchone()
    # 24 days old -> stale even though the tool first saw it today
    assert (blue["eligibility"], blue["eligibility_reason"]) == ("ineligible", "stale")
    shortkit = conn.execute("SELECT * FROM jobs WHERE company_norm='shortkit'").fetchone()
    assert shortkit["eligibility"] == "ineligible" and shortkit["eligibility_reason"] == "stale"   # 23 days
    hy = conn.execute("SELECT * FROM jobs WHERE company_norm='hylight'").fetchone()
    assert (hy["eligibility"], hy["bucket"]) == ("stretch", "us_auth_stretch")   # 5 days, US visa only
    assert hy["route"] in ("network", "skipped")
