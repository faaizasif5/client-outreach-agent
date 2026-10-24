import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from db import repo
from pipeline import scorer
from pipeline.claude_cli import ClaudeError
from schemas.ai_output import JobScore, ScoreBatch
from tests.conftest import NOW, make_job

BASE = {"job_key": "k1", "resume": "fullstack", "fit_score": 82, "evidence": ["Listing: x"],
        "reason": "r", "confidence": "high", "skip": False, "skip_reasons": []}


@pytest.mark.parametrize("change", [
    {"resume": "golang"}, {"fit_score": 120}, {"fit_score": -1}, {"evidence": []},
    {"evidence": ["a", "b", "c", "d"]}, {"reason": "x" * 201}, {"confidence": "sure"},
    {"skip_reasons": ["bored"]},
])
def test_invalid_scores_rejected(change):
    with pytest.raises(ValidationError):
        JobScore.model_validate({**BASE, **change})


def test_duplicate_job_keys_rejected():
    with pytest.raises(ValidationError):
        ScoreBatch.model_validate({"results": [BASE, BASE]})


@pytest.fixture
def prof(profile, tmp_path):
    (tmp_path / "fullstack.pdf").write_bytes(b"%PDF")
    (tmp_path / "python_django.pdf").write_bytes(b"%PDF")
    profile["resumes"]["fullstack"]["file"] = str(tmp_path / "fullstack.pdf")
    profile["resumes"]["python_django"]["file"] = str(tmp_path / "python_django.pdf")
    profile["resumes"]["fde"]["file"] = str(tmp_path / "missing_fde.pdf")
    return profile


def test_resume_without_pdf_falls_back_to_fullstack(prof):
    assert scorer.resolve_resume("fde", prof)[0] == "fullstack"
    assert scorer.resolve_resume("python_django", prof)[0] == "python_django"
    prof["resumes"]["fullstack"]["file"] = "nope.pdf"
    key, path = scorer.resolve_resume("fde", prof)
    assert path is None   # never selected without a real PDF


def test_trim_prefers_requirements():
    text = "About us. " * 200 + "\nRequirements: Django, Postgres\n" + "More. " * 50
    out = scorer.trim_description(text, 600)
    assert "Requirements: Django" in out and len(out) <= 600


def _job(conn, key, company="acme", eligibility="eligible", bucket="eligible", route="email",
         days_old=2, **kw):
    make_job(conn, job_key=key, company=company.title(), company_norm=company,
             eligibility=eligibility, bucket=bucket, route=route,
             posted_at=repo.iso(NOW - timedelta(days=days_old)), description="Django role", **kw)


def fake_claude(results):
    def call(prompt, model, schema, retries, timeout, **kw):
        return schema.model_validate({"results": results}), {"usage": {"input_tokens": 10}}
    return call


def _jobs(conn, keys):
    return [dict(conn.execute("SELECT * FROM jobs WHERE job_key=?", (k,)).fetchone(), kw_score=1)
            for k in keys]


def test_score_jobs_postprocessing(conn, cfg, prof):
    for k in ("a", "b", "c"):
        _job(conn, k, company=f"co{k}")
    results = [
        {**BASE, "job_key": "a", "resume": "fde"},
        {**BASE, "job_key": "b", "skip": True, "skip_reasons": ["hidden_location_restriction"]},
        {**BASE, "job_key": "zzz"},                    # unknown -> rejected
    ]                                                  # c missing -> score_missing
    summary = scorer.score_jobs(conn, _jobs(conn, "abc"), prof, cfg, "run1", NOW,
                                claude=fake_claude(results))
    assert summary["scored"] == 2 and summary["missing"] == 1 and summary["rejected_keys"] == ["zzz"]
    ev = conn.execute("SELECT * FROM evaluations WHERE job_key='a'").fetchone()
    assert (ev["resume_key"], ev["model_resume_key"]) == ("fullstack", "fde")
    assert repo.get_job(conn, "b")["eligibility"] == "ineligible"
    assert repo.get_job(conn, "c")["status"] == "score_missing"
    assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE job_key='zzz'").fetchone()[0] == 0


def test_claude_failure_marks_jobs_missing(conn, cfg, prof):
    _job(conn, "a")

    def boom(*a, **k):
        raise ClaudeError("down")
    summary = scorer.score_jobs(conn, _jobs(conn, "a"), prof, cfg, "run1", NOW, claude=boom)
    assert summary["error"] and repo.get_job(conn, "a")["status"] == "score_missing"


def _evaluate(conn, key, fit, confidence="high", skip=False, resume="fullstack"):
    conn.execute("INSERT INTO evaluations (job_key, run_id, resume_key, fit_score, evidence, reason, "
                 "confidence, skip, skip_reasons, evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (key, "r1", resume, fit, json.dumps(["e"]), "r", confidence, int(skip), "[]",
                  repo.iso(NOW)))


def decisions(conn, cfg, prof):
    return {i["job_key"]: i["decision"] for i in scorer.shortlist(conn, cfg, prof, NOW)}


def test_bands():
    cfg = {"scoring": {"strong_fit": 80, "min_fit": 60}}
    assert [scorer.band(f, cfg) for f in (95, 80, 79, 60, 59)] == ["strong", "strong", "possible", "possible", "skip"]


def test_shortlist_rules(conn, cfg, prof):
    _job(conn, "strong", company="a"); _evaluate(conn, "strong", 85)
    _job(conn, "weak", company="b"); _evaluate(conn, "weak", 55)
    _job(conn, "lowconf", company="c"); _evaluate(conn, "lowconf", 90, confidence="low")
    _job(conn, "aiskip", company="d"); _evaluate(conn, "aiskip", 90, skip=True)
    _job(conn, "stretch65", company="e", eligibility="stretch", bucket="us_auth_stretch")
    _evaluate(conn, "stretch65", 65)
    _job(conn, "stretch75", company="f", eligibility="stretch", bucket="us_auth_stretch")
    _evaluate(conn, "stretch75", 75)
    _job(conn, "old", company="g", days_old=45); _evaluate(conn, "old", 95)
    _job(conn, "inel", company="h", eligibility="ineligible"); _evaluate(conn, "inel", 95)
    _job(conn, "nopdf", company="i"); _evaluate(conn, "nopdf", 85, resume="dotnet")
    prof["resumes"]["fullstack"]["file"] = str(prof["resumes"]["fullstack"]["file"])
    d = decisions(conn, cfg, prof)
    assert d["strong"] == "shortlisted" and d["stretch75"] == "shortlisted"
    assert d["weak"] == "below_threshold" and d["stretch65"] == "below_threshold"
    assert d["lowconf"] == "low_confidence" and d["aiskip"] == "skipped_by_ai"
    assert d["old"] == "stale" and d["inel"] == "not_eligible"
    assert d["nopdf"] == "shortlisted"   # dotnet has no PDF -> falls back to fullstack


def test_caps_and_ranking(conn, cfg, prof):
    cfg["caps"]["portal_kits"] = 2
    for n, fit in enumerate([70, 95, 85, 75]):
        _job(conn, f"p{n}", company=f"c{n}", route="portal"); _evaluate(conn, f"p{n}", fit)
    items = scorer.shortlist(conn, cfg, prof, NOW)
    assert [(i["job_key"], i["decision"]) for i in items] == [
        ("p1", "shortlisted"), ("p2", "shortlisted"), ("p3", "queued"), ("p0", "queued")]


def test_hard_max_clamps_caps(conn, cfg, prof):
    cfg["caps"]["portal_kits"] = 50
    for n in range(7):
        _job(conn, f"p{n}", company=f"c{n}", route="portal"); _evaluate(conn, f"p{n}", 90)
    shortlisted = [i for i in scorer.shortlist(conn, cfg, prof, NOW) if i["decision"] == "shortlisted"]
    assert len(shortlisted) == cfg["hard_max"]["portal_kits"]


def test_explicit_us_only_ranked_last_and_ties_prefer_eligible(conn, cfg, prof):
    _job(conn, "x", company="a", eligibility="stretch", bucket="explicit_us_only"); _evaluate(conn, "x", 99)
    _job(conn, "u", company="b", eligibility="stretch", bucket="us_auth_stretch"); _evaluate(conn, "u", 80)
    _job(conn, "e", company="c"); _evaluate(conn, "e", 80)
    _job(conn, "r", company="d", eligibility="stretch", bucket="remote_us_stretch"); _evaluate(conn, "r", 80)
    order = [i["job_key"] for i in scorer.shortlist(conn, cfg, prof, NOW)]
    assert order == ["e", "r", "u", "x"]


def test_one_job_per_company_in_shortlist(conn, cfg, prof):
    _job(conn, "a1", company="acme"); _evaluate(conn, "a1", 90)
    _job(conn, "a2", company="acme", route="portal"); _evaluate(conn, "a2", 85)
    d = decisions(conn, cfg, prof)
    assert d == {"a1": "shortlisted", "a2": "suppressed_company"}


def test_company_with_outreach_is_suppressed(conn, cfg, prof):
    _job(conn, "old_app", company="acme")
    repo.create_application(conn, job_key="old_app", outreach_type="application_brief",
                            bucket="eligible", resume_key="fullstack", now=NOW)
    _job(conn, "new", company="acme"); _evaluate(conn, "new", 95)
    assert decisions(conn, cfg, prof)["new"] == "suppressed_company"
    assert repo.get_job(conn, "new")["status"] == "suppressed_company"


def test_stretch_jobs_never_skipped_for_the_known_us_requirement(conn, cfg, prof):
    _job(conn, "s1", company="a", eligibility="stretch", bucket="us_auth_stretch")
    _job(conn, "s2", company="b", eligibility="stretch", bucket="us_auth_stretch")
    _job(conn, "e1", company="c")
    results = [
        {**BASE, "job_key": "s1", "skip": True, "skip_reasons": ["hidden_location_restriction"]},
        {**BASE, "job_key": "s2", "skip": True, "skip_reasons": ["hidden_location_restriction", "too_senior"]},
        {**BASE, "job_key": "e1", "skip": True, "skip_reasons": ["hidden_location_restriction"]},
    ]
    jobs = _jobs(conn, ["s1", "s2", "e1"])
    scorer.score_jobs(conn, jobs, prof, cfg, "run1", NOW, claude=fake_claude(results))
    ev = {r["job_key"]: r for r in conn.execute("SELECT * FROM evaluations")}
    assert ev["s1"]["skip"] == 0 and json.loads(ev["s1"]["skip_reasons"]) == []
    assert ev["s2"]["skip"] == 1 and json.loads(ev["s2"]["skip_reasons"]) == ["too_senior"]
    assert repo.get_job(conn, "s1")["eligibility"] == "stretch"          # not knocked out
    assert repo.get_job(conn, "e1")["eligibility"] == "ineligible"       # eligible bucket: still applies


def test_score_prompt_tells_claude_the_bucket(prof, cfg):
    job = {"job_key": "k", "company": "A", "title": "Eng", "location_text": "Remote (US)", "trimmed": "x",
           "kw_score": 1, "bucket": "us_auth_stretch"}
    prompt, _ = scorer.build_prompt([job], prof, cfg)
    assert "bucket: us_auth_stretch" in prompt and "never skip or lower the score" in prompt
