"""Claude call #1: score + pick resume (spec section 10), and the code-only shortlist rules."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from db import repo
from pipeline.claude_cli import ClaudeError, run_claude
from schemas.ai_output import ScoreBatch

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "prompts" / "score.md"
SECTION_START = re.compile(
    r"^.{0,40}\b(requirements|responsibilit|qualifications|what you.ll do|what we.re looking for|"
    r"you have|about you|you will|the role|skills)\b", re.I | re.M)
BUCKET_PRIORITY = {"eligible": 0, "remote_us_stretch": 1, "us_auth_stretch": 2, "explicit_us_only": 3}
ROUTE_CAP = {"email": "email_drafts", "portal": "portal_kits", "network": "linkedin_requests"}


def band(fit: int, cfg: dict) -> str:
    if fit >= cfg["scoring"]["strong_fit"]:
        return "strong"
    if fit >= cfg["scoring"]["min_fit"]:
        return "possible"
    return "skip"


def trim_description(text: str, limit: int) -> str:
    """Prefer the requirements/responsibilities part; ~400 tokens."""
    text = text or ""
    m = SECTION_START.search(text)
    if m and m.start() > 0 and len(text) > limit:
        head = text[:300]
        return (head + "\n...\n" + text[m.start():])[:limit]
    return text[:limit]


# ---------------------------------------------------------------- resumes

def resume_keys(profile: dict) -> list[str]:
    return [k for k, v in (profile.get("resumes") or {}).items() if isinstance(v, dict) and v.get("summary")]


def resolve_resume(chosen: str, profile: dict, root: Path = ROOT) -> tuple[str, str | None]:
    """A key without a real PDF on disk is never selected: fall back to fullstack, else none."""
    resumes = profile.get("resumes") or {}
    for key in (chosen, "fullstack"):
        entry = resumes.get(key)
        if isinstance(entry, dict) and entry.get("file") and (root / entry["file"]).is_file():
            return key, entry["file"]
    return chosen, None


# ---------------------------------------------------------------- prompt

def candidate_section(profile: dict) -> str:
    lines = [f"Location: {profile.get('location')}",
             f"Arrangement: {', '.join(profile.get('availability', {}).get('arrangement', []))}",
             f"Years of experience: {profile.get('answer_bank', {}).get('years_experience')}"]
    for exp in profile.get("experience") or []:
        lines.append(f"Experience: {exp['title']} at {exp['company']} ({exp['period']}). "
                     + " ".join(exp.get("facts") or []))
    for key, proj in (profile.get("projects") or {}).items():
        lines.append(f"Project {key} ({proj.get('type')}): stack {', '.join(proj.get('stack') or [])}. "
                     + " ".join(proj.get("facts") or []))
    skills = profile.get("skills") or {}
    for group in ("production", "projects", "other"):
        lines.append(f"Skills ({group}): {', '.join(skills.get(group) or [])}")
    for key in resume_keys(profile):
        lines.append(f"Resume {key}: {profile['resumes'][key]['summary']}")
    return "\n".join(lines)


def job_block(job) -> str:
    return (f"<listing job_key=\"{job['job_key']}\">\nbucket: {job.get('bucket') or 'eligible'}\n"
            f"Company: {job['company']}\nTitle: {job['title']}\n"
            f"Location: {job['location_text'] or ''}\n{job['trimmed']}\n</listing>")


def build_prompt(jobs: list[dict], profile: dict, cfg: dict) -> tuple[str, list[dict]]:
    """Fill the template; drop lowest keyword-score jobs until under the size budget."""
    template = PROMPT_FILE.read_text(encoding="utf-8")
    jobs = sorted(jobs, key=lambda j: -j["kw_score"])
    while True:
        prompt = template.format(
            years=profile.get("answer_bank", {}).get("years_experience", "?"),
            resume_keys=", ".join(resume_keys(profile)),
            candidate=candidate_section(profile),
            jobs="\n\n".join(job_block(j) for j in jobs))
        if len(prompt) <= cfg["scoring"]["prompt_char_budget"] or len(jobs) <= 1:
            return prompt, jobs
        jobs = jobs[:-1]


# ---------------------------------------------------------------- scoring

def score_jobs(conn, jobs: list[dict], profile: dict, cfg: dict, run_id: str, now: datetime,
               claude=run_claude) -> dict:
    """Score up to scoring.max_jobs jobs in one call. Returns a summary incl. CLI usage."""
    sc = cfg["scoring"]
    jobs = [dict(j, trimmed=trim_description(j["description"], sc["description_chars"]))
            for j in jobs[:sc["max_jobs"]]]
    prompt, jobs = build_prompt(jobs, profile, cfg)
    sent = {j["job_key"] for j in jobs}
    try:
        batch, usage = claude(prompt, sc["model"], ScoreBatch, retries=sc["retries"],
                              timeout=sc["timeout_seconds"], effort=sc.get("effort"))
    except ClaudeError as e:
        for key in sent:
            repo.update_job(conn, key, {"status": "score_missing"})
        return {"sent": len(sent), "scored": 0, "error": str(e), "usage": getattr(e, "usage", None),
                "prompt_chars": len(prompt)}
    scored, rejected = set(), []
    allowed = set(resume_keys(profile))
    bucket_of = {j["job_key"]: j.get("bucket") for j in jobs}
    for r in batch.results:
        if r.job_key not in sent:
            rejected.append(r.job_key)          # unknown job_key -> rejected
            continue
        if bucket_of.get(r.job_key) not in (None, "eligible"):
            # Stretch jobs are US-focused by design (pitched as a contractor alternative): the US
            # requirement is never a reason to skip them. Other skip reasons still count.
            reasons = [s for s in r.skip_reasons if s.value != "hidden_location_restriction"]
            if len(reasons) != len(r.skip_reasons):
                r.skip_reasons = reasons
                r.skip = bool(reasons)
        model_key = r.resume.value
        resume_key, _ = resolve_resume(model_key if model_key in allowed else "fullstack", profile)
        conn.execute(
            "INSERT OR IGNORE INTO evaluations (job_key, run_id, resume_key, fit_score, evidence, "
            "reason, confidence, skip, skip_reasons, evaluated_at, model_resume_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r.job_key, run_id, resume_key, r.fit_score, json.dumps(r.evidence), r.reason,
             r.confidence.value, int(r.skip), json.dumps([s.value for s in r.skip_reasons]),
             repo.iso(now), model_key))
        update = {"status": "scored"}
        if "hidden_location_restriction" in [s.value for s in r.skip_reasons]:
            update.update(eligibility="ineligible", eligibility_reason="hidden_location_restriction")
        repo.update_job(conn, r.job_key, update)
        scored.add(r.job_key)
    for key in sent - scored:
        repo.update_job(conn, key, {"status": "score_missing"})   # retried next run
    return {"sent": len(sent), "scored": len(scored), "missing": len(sent - scored),
            "rejected_keys": rejected, "usage": usage, "prompt_chars": len(prompt)}


# ---------------------------------------------------------------- shortlist (code, not AI)

def latest_evaluations(conn):
    return conn.execute(
        "SELECT j.*, e.resume_key, e.model_resume_key, e.fit_score, e.evidence, e.reason, "
        "e.confidence, e.skip, e.skip_reasons, e.evaluated_at, e.run_id "
        "FROM jobs j JOIN evaluations e ON e.id = (SELECT id FROM evaluations WHERE job_key = j.job_key "
        "ORDER BY evaluated_at DESC, id DESC LIMIT 1)").fetchall()


def _cap(cfg, route):
    name = ROUTE_CAP[route]
    cap = cfg["caps"][name]
    return min(cap, cfg["hard_max"][name]) if name in cfg["hard_max"] else cap


def shortlist(conn, cfg: dict, profile: dict, now: datetime, root: Path = ROOT) -> list[dict]:
    """Every evaluated job with its decision: shortlisted | queued | below_threshold |
    low_confidence | skipped_by_ai | no_resume | suppressed_company | not_eligible | stale."""
    max_age = timedelta(days=cfg["max_age_days"])
    items = []
    for row in latest_evaluations(conn):
        item = dict(row)
        item["evidence"] = json.loads(row["evidence"])
        item["skip_reasons"] = json.loads(row["skip_reasons"] or "[]")
        item["band"] = band(row["fit_score"], cfg)
        _, item["resume_path"] = resolve_resume(row["resume_key"], profile, root)
        posted = repo.parse_iso(row["posted_at"])
        min_fit = cfg["stretch_min_fit"] if row["eligibility"] == "stretch" else cfg["scoring"]["min_fit"]
        has_outreach = conn.execute("SELECT 1 FROM applications WHERE job_key=?", (row["job_key"],)).fetchone()
        if row["eligibility"] not in ("eligible", "stretch") or row["lifecycle"] != "open" or has_outreach:
            item["decision"] = "not_eligible"
        elif not posted or now - posted > max_age:
            item["decision"] = "stale"          # never by first_seen_at
        elif row["skip"]:
            item["decision"] = "skipped_by_ai"
        elif row["confidence"] == "low":
            item["decision"] = "low_confidence"   # -> uncertain list
        elif row["fit_score"] < min_fit:
            item["decision"] = "below_threshold"
        elif not item["resume_path"]:
            item["decision"] = "no_resume"
        else:
            item["decision"] = "candidate"
        items.append(item)

    candidates = sorted(
        (i for i in items if i["decision"] == "candidate"),
        key=lambda i: (i["bucket"] == "explicit_us_only", -i["fit_score"],
                       BUCKET_PRIORITY.get(i["bucket"], 9)))
    taken_companies: list[tuple[str, str | None]] = []
    used = {r: 0 for r in ROUTE_CAP}
    for rank, item in enumerate(candidates):
        item["rank"] = rank
        same_run = any(item["company_norm"] == n or (item["company_domain"] and item["company_domain"] == d)
                       for n, d in taken_companies)
        blocked = repo.company_block_reason(conn, item["company_norm"], item["company_domain"])
        if same_run or blocked:
            item["decision"] = "suppressed_company"
            item["suppressed_by"] = blocked or "same run"
            repo.update_job(conn, item["job_key"], {"status": "suppressed_company"})
            continue
        taken_companies.append((item["company_norm"], item["company_domain"]))
        if used[item["route"]] < _cap(cfg, item["route"]):
            used[item["route"]] += 1
            item["decision"] = "shortlisted"
        else:
            item["decision"] = "queued"
    order = {"shortlisted": 0, "queued": 1}
    return sorted(items, key=lambda i: (order.get(i["decision"], 2), i.get("rank", 0), -i["fit_score"]))
