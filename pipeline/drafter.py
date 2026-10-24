"""Claude call #2: draft emails and application kits (spec section 11), plus code-side guards.

Claude only returns text. Code decides outreach type, recipient, attachment and caps, and
rejects any draft that breaks a truthfulness rule (numbers not in the profile, implied US work
authorization, rates, scores, foreign links)."""
import json
import re
from pathlib import Path

from pipeline.claude_cli import ClaudeError, run_claude
from schemas.ai_output import DraftBatch

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "prompts" / "draft.md"

NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
URL_OR_EMAIL = re.compile(r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
# First-person claims only: acknowledging what the listing asks for ("the role asks for
# candidates authorized to work in the US") is required for stretch pitches and is fine.
US = r"(us|u\.s\.|usa|united states|america)"
US_AUTH_CLAIM = re.compile(
    rf"\b(i|i'm|i am|i've|i have)\b[^.;!?]{{0,40}}?\b("
    rf"(authori[sz]ed|eligible|permitted|cleared) to work in the {US}|"
    rf"(a )?{US} (citizen|national|permanent resident|resident)|green card|"
    rf"(based|located|living|residing|reside|relocat\w*) in the {US})\b", re.I)
NEGATED = re.compile(r"\bnot\b|n't\b|\bnever\b", re.I)
SCORE_LEAK = re.compile(r"\bfit[ _]?score\b|\bscore[ds]?\b|\bevidence\b|\bshortlist", re.I)
RATE = re.compile(r"[$€£]\s?\d|\b\d+\s?(usd|eur|gbp|/hr|/hour|per hour|an hour|k/yr|k per year)\b", re.I)
COST = re.compile(r"\bcost[- ]?effective\b|\baffordable\b|\bcheaper\b|\blower[- ]cost\b|\bsav(e|ings) on cost", re.I)
TRIAL = re.compile(r"\b(paid )?trial\b", re.I)
WORK_AUTH_QUESTION = re.compile(r"authori[sz]|sponsor|visa|citizen|legally|work permit|right to work", re.I)


# ---------------------------------------------------------------- prompt

def candidate_facts(profile: dict, resume_key: str) -> str:
    resumes = profile.get("resumes") or {}
    resume = resumes.get(resume_key) or resumes.get("fullstack") or {}
    lines = [f"Name: {profile['name']}", f"Location: {profile['location']}",
             f"Arrangement: {', '.join(profile['availability']['arrangement'])}; "
             f"US overlap: {profile['availability']['us_overlap_hours']} hours/day",
             f"Years of experience: {profile.get('answer_bank', {}).get('years_experience')}",
             f"Links: {json.dumps(profile.get('links') or {})}"]
    for exp in profile.get("experience") or []:
        lines.append(f"Employer: {exp['title']} at {exp['company']} ({exp['period']}; {exp.get('context', '')})")
        lines += [f"  fact: {f}" for f in exp.get("facts") or []]
    projects = profile.get("projects") or {}
    for key in resume.get("highlight_projects") or list(projects):
        proj = projects.get(key)
        if proj:
            lines.append(f"Project {key} ({proj['type']}), role {proj['role']}, stack {', '.join(proj['stack'])}")
            lines += [f"  fact: {f}" for f in proj.get("facts") or []]
    skills = profile.get("skills") or {}
    for group in ("production", "projects", "other"):
        lines.append(f"Skills {group}: {', '.join(skills.get(group) or [])}")
    for edu in profile.get("education") or []:
        lines.append(f"Education: {edu}")
    return "\n".join(lines)


def job_block(job: dict, description_chars: int) -> str:
    questions = "\n".join(f"  - {q}" for q in job.get("form_questions") or [])
    return (f'<listing job_key="{job["job_key"]}">\n'
            f"outreach_type: {job['outreach_type']}\nbucket: {job['bucket']}\n"
            f"pitch_variant: {job.get('pitch_variant') or 'n/a'}\nresume: {job['resume_key']}\n"
            f"Company: {job['company']}\nTitle: {job['title']}\nLocation: {job.get('location_text') or ''}\n"
            + (f"Form questions:\n{questions}\n" if questions else "")
            + f"{(job.get('description') or '')[:description_chars]}\n</listing>")


def build_prompt(jobs: list[dict], profile: dict, cfg: dict) -> str:
    template = PROMPT_FILE.read_text(encoding="utf-8")
    trial = (" Also offer a short paid trial on a real task." if cfg.get("offer_paid_trial") else "")
    # One shared facts block per resume key in use, so each job sees the right highlights.
    keys = sorted({j["resume_key"] for j in jobs})
    candidate = "\n\n".join(f"[resume {k}]\n{candidate_facts(profile, k)}" for k in keys)
    return template.format(overlap=profile["availability"]["us_overlap_hours"], trial=trial,
                           candidate=candidate,
                           jobs="\n\n".join(job_block(j, cfg["drafting"]["description_chars"]) for j in jobs))


# ---------------------------------------------------------------- guards (code, not AI)

def _profile_text(profile: dict) -> str:
    parts = [str(profile.get("answer_bank", {}).get("years_experience", "")),
             str(profile["availability"]["us_overlap_hours"])]
    for exp in profile.get("experience") or []:
        parts += exp.get("facts") or []
        parts.append(exp.get("period", ""))
    for proj in (profile.get("projects") or {}).values():
        parts += proj.get("facts") or []
        parts += proj.get("stack") or []
    for group in (profile.get("skills") or {}).values():
        parts += group or []
    parts += profile.get("education") or []
    return " ".join(parts)


def _own_links(profile: dict) -> set[str]:
    values = list((profile.get("links") or {}).values()) + [profile.get("answer_bank", {}).get("email", "")]
    return {v.lower().rstrip("/") for v in values if v and v != "..."}


def validate_draft(d, job: dict, profile: dict, cfg: dict) -> list[str]:
    """Problems that make a draft unusable. Empty list = accepted."""
    problems = []
    if d.outreach_type.value != job["outreach_type"]:
        problems.append(f"outreach_type {d.outreach_type.value} != {job['outreach_type']}")
    if job["outreach_type"] == "direct_email":
        if not (d.subject and d.body.strip()):
            problems.append("missing subject or body")
        lines = [l for l in d.body.splitlines() if l.strip()]
        if len(lines) > 12:
            problems.append(f"body too long ({len(lines)} lines)")
    elif not d.kit:
        problems.append("portal job without kit")
    if job["bucket"] != "eligible" and job["outreach_type"] != "direct_email":
        problems.append("stretch job must never get a portal kit")

    texts = [d.subject or "", d.body]
    if d.kit:
        texts += [d.kit.cover_note] + [a.answer for a in d.kit.form_answers]
    text = "\n".join(texts)

    allowed_numbers = set(NUMBER.findall(_profile_text(profile))) | set(
        NUMBER.findall(f"{job.get('title', '')} {job.get('description', '')}"))
    bad_numbers = sorted({n for n in NUMBER.findall(text) if n not in allowed_numbers})
    if bad_numbers:
        problems.append(f"numbers not in profile or listing: {bad_numbers}")
    own = _own_links(profile)
    foreign = [u for u in URL_OR_EMAIL.findall(text) if u.lower().rstrip("/.,)") not in own]
    if foreign:
        problems.append(f"links/emails not from profile: {foreign}")
    for m in US_AUTH_CLAIM.finditer(text):
        if not NEGATED.search(m.group(0)):
            problems.append(f"implies US work authorization/residence: '{m.group(0)}'")
    if SCORE_LEAK.search(text):
        problems.append("mentions scores/evidence")
    if RATE.search(text):
        problems.append("names a rate or price")
    if job["bucket"] != "eligible":
        if "pakistan" not in d.body.lower():
            problems.append("stretch email must say the candidate is based in Pakistan")
        cost_mentions = len(COST.findall(d.body))
        if job.get("pitch_variant") == "b" and cost_mentions:
            problems.append("variant b must not mention cost")
        if job.get("pitch_variant") == "a" and cost_mentions > 1:
            problems.append("variant a mentions cost more than once")
    if not cfg.get("offer_paid_trial") and TRIAL.search(text):
        problems.append("offers a trial while offer_paid_trial is off")
    return problems


# ---------------------------------------------------------------- kits: code-filled parts

def standard_fields(profile: dict) -> str:
    """Copy-paste answers from answer_bank. Work authorization is always the truthful text."""
    ab = profile.get("answer_bank") or {}
    links = profile.get("links") or {}
    rows = [("Email", ab.get("email")), ("Phone", ab.get("phone")),
            ("Years of experience", ab.get("years_experience")), ("Notice period", ab.get("notice_period")),
            ("Expected rate", ab.get("expected_rate")), ("Work authorization", ab.get("work_authorization")),
            ("GitHub", links.get("github")), ("LinkedIn", links.get("linkedin")),
            ("Portfolio", links.get("portfolio"))]
    return "\n".join(f"{k}: {v}" for k, v in rows if v not in (None, "", "..."))


def split_questions(questions: list[str]) -> tuple[list[str], list[str]]:
    """(questions for Claude, work-authorization questions answered by code)."""
    auth = [q for q in questions if WORK_AUTH_QUESTION.search(q)]
    return [q for q in questions if q not in auth], auth


def greenhouse_questions(http, apply_url: str | None) -> list[str]:
    """Custom application questions from the public Greenhouse API (no login)."""
    from pipeline.normalize import ats_board
    board = ats_board(apply_url)
    if not board or board[0] != "greenhouse":
        return []
    try:
        data = http.get(f"https://boards-api.greenhouse.io/v1/boards/{board[1]}/jobs/{board[2]}?questions=true",
                        as_json=True, ttl_hours=24)
    except Exception:
        return []
    skip = re.compile(r"^(first name|last name|email|phone|resume|cv|cover letter|linkedin|website)", re.I)
    return [q["label"] for q in data.get("questions", []) if q.get("label") and not skip.search(q["label"])][:6]


# ---------------------------------------------------------------- the call

def draft_jobs(jobs: list[dict], profile: dict, cfg: dict, claude=run_claude) -> dict:
    """jobs: dicts with job_key, outreach_type, bucket, resume_key, pitch_variant, company,
    title, location_text, description, form_questions. Returns accepted / rejected / usage."""
    dc = cfg["drafting"]
    for j in jobs:
        j["form_questions"], j["auth_questions"] = split_questions(j.get("form_questions") or [])
    prompt = build_prompt(jobs, profile, cfg)
    by_key = {j["job_key"]: j for j in jobs}
    try:
        batch, usage = claude(prompt, dc["model"], DraftBatch, retries=dc["retries"],
                              timeout=dc["timeout_seconds"], effort=dc.get("effort"))
    except ClaudeError as e:
        return {"accepted": {}, "rejected": {}, "missing": list(by_key), "error": str(e),
                "usage": getattr(e, "usage", None), "prompt_chars": len(prompt)}
    accepted, rejected = {}, {}
    for d in batch.results:
        job = by_key.get(d.job_key)
        if not job:
            continue   # unknown job_key -> ignored
        problems = validate_draft(d, job, profile, cfg)
        if problems:
            rejected[d.job_key] = problems
        else:
            accepted[d.job_key] = d
    missing = [k for k in by_key if k not in accepted and k not in rejected]
    return {"accepted": accepted, "rejected": rejected, "missing": missing, "usage": usage,
            "prompt_chars": len(prompt)}
