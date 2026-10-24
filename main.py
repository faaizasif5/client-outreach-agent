"""Daily pipeline entry point (Phase 1: discovery + matching, local report only).

    python main.py --dry-run          fetch live sources, gate, score, write reports/<date>.md
    python main.py label [files...]   read labels back from reports and print calibration
"""
import argparse
import json
import logging
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from db import repo
from fetchers import hn_whos_hiring, remoteok, wwr
from fetchers.detail_fetch import Http
from fetchers.models import RawJob
from outputs import digest
from pipeline import contacts as contacts_mod
from pipeline import dedupe, eligibility, freshness, keyword_match, normalize, scorer
from pipeline.claude_cli import build_command, run_claude

ROOT = Path(__file__).resolve().parent
PKT = timezone(timedelta(hours=5))   # Asia/Karachi has no DST; avoids a tzdata dependency on Windows
log = logging.getLogger("job_agent")


def load_config(root: Path = ROOT) -> dict:
    return yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))


def load_profile(root: Path = ROOT, path: Path | None = None) -> dict:
    path = path or root / "candidate" / "profile.yaml"
    if not path.exists():
        sys.exit("candidate/profile.yaml not found. Copy candidate/profile.example.yaml and fill it in.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- per-listing processing

def _company_domain(raw: RawJob) -> str | None:
    for value in (raw.company_url, raw.apply_url, *raw.links):
        domain = normalize.company_domain(value)
        if domain and not (value or "").lower().startswith("mailto:"):
            return domain
    return None


def process_raw(conn, raw: RawJob, cfg: dict, profile: dict, now: datetime, http: Http | None,
                newest_first_since: datetime | None = None) -> str:
    domain = _company_domain(raw)
    explicit_apply = raw.apply_url if raw.source != "hn" else None
    contacts = contacts_mod.extract_contacts(raw.description, raw.links, source=raw.source,
                                             company_domain=domain, explicit_apply_url=explicit_apply)
    if not domain and contacts.recruiting_email:
        domain = normalize.company_domain(contacts.recruiting_email)
    apply_canon = normalize.canonical_url(contacts.apply_url)
    team_size = eligibility.parse_team_size(raw.description)
    cand = dedupe.Candidate(
        source=raw.source, external_id=raw.external_id, source_url=raw.url, company=raw.company,
        company_norm=normalize.norm_company(raw.company), company_domain=domain, title=raw.title,
        title_norm=normalize.norm_title(raw.title), canonical_url=normalize.canonical_url(raw.url),
        ats_key=normalize.ats_key(contacts.apply_url) or next(
            (k for k in map(normalize.ats_key, raw.links) if k), None),
        apply_url=apply_canon, posted_at=raw.posted_at,
        fields={"location_text": raw.location_text, "description": raw.description,
                "contact_email": contacts.recruiting_email, "team_size": team_size,
                "founder_links": json.dumps(contacts.founder_links)})
    job_key, _, _ = dedupe.upsert(conn, cand, now)
    row = repo.get_job(conn, job_key)

    # Freshness check #1 on the merged row: the earliest posted date known for this job.
    posted = repo.parse_iso(row["posted_at"])
    ats_lookup = (lambda: http.ats_posted_date(row["apply_url"])) if http and not posted else None
    age = freshness.assess(posted, raw.description, now, cfg["max_age_days"], ats_date=ats_lookup,
                           newest_first_since=newest_first_since)
    closed = freshness.is_closed_text(f"{raw.description}\n{raw.replies_text}", cfg["eligibility"]["closed"])

    merged_contacts = contacts_mod.Contacts(
        recruiting_email=row["contact_email"], apply_url=row["apply_url"],
        founder_links=json.loads(row["founder_links"] or "[]"), preference=contacts.preference,
        flags=contacts.flags)
    result = eligibility.evaluate(
        title=row["title"], location_text=raw.location_text, description=raw.description,
        contacts=merged_contacts, team_size=row["team_size"], age_status=age.status, closed=closed,
        cfg=cfg, overlap_hours=profile["availability"]["us_overlap_hours"])

    flags = json.loads(row["flags"] or "[]")
    for f in result.flags + contacts.flags + (["date_estimated"] if age.estimated else []):
        if f not in flags:
            flags.append(f)
    updates = {"flags": json.dumps(flags)}
    if age.posted_at and not row["posted_at"]:
        updates.update(posted_at=repo.iso(age.posted_at), posted_at_source=age.source)
    if closed:
        updates["lifecycle"] = "closed"
    if row["eligibility_reason"] != "hidden_location_restriction":   # set by scoring; sticky
        updates.update(eligibility=result.eligibility, eligibility_reason=result.reason,
                       bucket=result.bucket, route=result.route)
    if row["status"] == "new" and not keyword_match.is_engineering_role(row["title"], cfg):
        updates["status"] = "keyword_filtered"
    repo.update_job(conn, job_key, updates)
    return job_key


def _previous_run_start(conn, run_id: str) -> datetime | None:
    row = conn.execute("SELECT MAX(started_at) FROM runs WHERE status='success' AND run_id != ?",
                       (run_id,)).fetchone()
    return repo.parse_iso(row[0]) if row and row[0] else None


def _already_seen(conn, raw: RawJob, run_started: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE first_seen_at < ? AND EXISTS (SELECT 1 FROM json_each(jobs.sources) "
        "WHERE json_extract(value,'$.source')=? AND json_extract(value,'$.external_id')=?)",
        (run_started, raw.source, raw.external_id)).fetchone() is not None


def ingest(conn, raws: list[RawJob], cfg, profile, now, http, run_id, errors) -> None:
    """Upsert listings in feed order, with newest-first evidence for undated items."""
    seen = [_already_seen(conn, r, repo.iso(now)) for r in raws]
    evidence = freshness.newest_first_evidence(seen)
    since = _previous_run_start(conn, run_id)
    for i, raw in enumerate(raws):
        try:
            process_raw(conn, raw, cfg, profile, now, http,
                        newest_first_since=since if i in evidence and not raw.posted_at else None)
        except Exception as e:   # one bad listing never stops the run
            log.exception("listing failed: %s %s", raw.source, raw.external_id)
            errors.append(f"{raw.source}:{raw.external_id}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- pipeline

def scoring_candidates(conn, cfg, profile, now, rescore: bool = False) -> list[dict]:
    """Unscored eligible/stretch jobs. rescore=True also re-scores already evaluated ones
    (manual model comparison / consistency check); the newest evaluation is the one used."""
    cutoff = repo.iso(now - timedelta(days=cfg["max_age_days"]))
    unscored = "" if rescore else         "AND status IN ('new','score_missing') AND job_key NOT IN (SELECT job_key FROM evaluations)"
    rows = conn.execute(
        "SELECT * FROM jobs WHERE eligibility IN ('eligible','stretch') AND lifecycle='open' "
        f"AND status != 'keyword_filtered' AND posted_at >= ? {unscored}", (cutoff,)).fetchall()
    out = []
    for r in rows:
        if not keyword_match.is_engineering_role(r["title"], cfg):
            repo.update_job(conn, r["job_key"], {"status": "keyword_filtered"})
            continue
        out.append(dict(r, kw_score=keyword_match.keyword_score(r["title"], r["description"], profile, cfg)))
    return sorted(out, key=lambda j: (-j["kw_score"], j["job_key"]))


def claude_ready() -> str | None:
    """None if the CLI is installed and logged in, else a short reason."""
    try:
        proc = subprocess.run([build_command("haiku")[0], "auth", "status"], capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
        status = json.loads(proc.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return f"claude CLI unavailable: {e}"
    return None if status.get("loggedIn") else "claude CLI not logged in (run `claude auth login`)"


def run_pipeline(conn, cfg: dict, profile: dict, *, now: datetime, fetchers: dict, claude=run_claude,
                 http: Http | None = None, report_dir: Path = ROOT / "reports",
                 check_claude=claude_ready, rescore: bool = False) -> dict:
    local = now.astimezone(PKT)
    run_id = local.strftime("%Y%m%dT%H%M%S%z")
    conn.execute("INSERT INTO runs (run_id, started_at, status) VALUES (?,?, 'running')",
                 (run_id, repo.iso(now)))
    errors: list[str] = []
    counts: Counter = Counter()
    for name, fetch in fetchers.items():
        try:
            raws = fetch()
        except Exception as e:   # one source failing never stops the others
            log.exception("source %s failed", name)
            errors.append(f"source {name}: {type(e).__name__}: {e}")
            continue
        counts[f"fetched_{name}"] = len(raws)
        ingest(conn, raws, cfg, profile, now, http, run_id, errors)

    candidates = scoring_candidates(conn, cfg, profile, now, rescore=rescore)
    counts["score_candidates"] = len(candidates)
    usage, status = None, "success"
    if candidates:
        problem = check_claude() if check_claude else None
        if problem:
            errors.append(f"scoring skipped: {problem}")
            status = "failed"
        else:
            summary = scorer.score_jobs(conn, candidates, profile, cfg, run_id, now, claude=claude)
            usage = summary.get("usage")
            counts["scored"] = summary["scored"]
            counts["prompt_chars"] = summary["prompt_chars"]
            if summary.get("error"):
                errors.append(f"scoring failed: {summary['error']}")
                status = "failed"
            if summary.get("rejected_keys"):
                errors.append(f"rejected unknown job_keys from Claude: {summary['rejected_keys']}")
    log.info("claude usage: %s", json.dumps(usage, default=str))

    items = scorer.shortlist(conn, cfg, profile, now)
    today = local.date().isoformat()
    todays = [i for i in items if i["decision"] in ("shortlisted", "queued") or
              repo.parse_iso(i["evaluated_at"]).astimezone(PKT).date().isoformat() == today]
    seen_now = conn.execute("SELECT * FROM jobs WHERE last_seen_at = ?", (repo.iso(now),)).fetchall()
    uncertain = [{"company": r["company"], "title": r["title"], "reason": r["eligibility_reason"],
                  "location_text": r["location_text"], "sources": r["sources"]}
                 for r in seen_now if r["eligibility"] == "uncertain"]
    uncertain += [{"company": i["company"], "title": i["title"], "reason": "low_confidence_score",
                   "location_text": i["location_text"], "sources": i["sources"]}
                  for i in todays if i["decision"] == "low_confidence"]
    ineligible = Counter(r["eligibility_reason"] for r in seen_now if r["eligibility"] == "ineligible")
    counts.update({
        "seen": len(seen_now),
        "eligible": sum(1 for r in seen_now if r["eligibility"] == "eligible"),
        "stretch": sum(1 for r in seen_now if r["eligibility"] == "stretch"),
        "uncertain": len(uncertain), "ineligible": sum(ineligible.values()),
        "shortlisted": sum(1 for i in items if i["decision"] == "shortlisted"),
        "queued": sum(1 for i in items if i["decision"] == "queued"),
        "http_requests": http.requests_made if http else 0,
    })
    existing = {r["job_key"]: r["label"] for r in conn.execute("SELECT job_key, label FROM labels")}
    report = digest.write_report(report_dir / f"{today}.md", {
        "date": today, "run_id": run_id, "counts": dict(counts), "usage": usage, "errors": errors,
        "items": todays, "uncertain": uncertain, "ineligible_counts": ineligible,
        "existing_labels": existing})
    conn.execute("UPDATE runs SET finished_at=?, status=?, counts=?, usage=?, errors=? WHERE run_id=?",
                 (repo.iso(datetime.now(timezone.utc)), status, json.dumps(dict(counts)),
                  json.dumps(usage, default=str), json.dumps(errors), run_id))
    return {"run_id": run_id, "status": status, "counts": dict(counts), "usage": usage,
            "errors": errors, "report": report}


# ---------------------------------------------------------------- label command

def label_command(conn, files: list[Path]) -> str:
    now = datetime.now(timezone.utc)
    stored = 0
    for f in files:
        for job_key, label in digest.read_labels(f.read_text(encoding="utf-8")).items():
            ev = conn.execute("SELECT fit_score, resume_key FROM evaluations WHERE job_key=? "
                              "ORDER BY evaluated_at DESC LIMIT 1", (job_key,)).fetchone()
            if not ev:
                continue
            conn.execute("INSERT INTO labels (job_key, label, fit_score, resume_key, report_file, labeled_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET label=excluded.label, "
                         "fit_score=excluded.fit_score, resume_key=excluded.resume_key, "
                         "report_file=excluded.report_file, labeled_at=excluded.labeled_at",
                         (job_key, label, ev["fit_score"], ev["resume_key"], f.name, repo.iso(now)))
            stored += 1
    rows = [dict(r) for r in conn.execute("SELECT label, fit_score, resume_key FROM labels")]
    return f"Read {stored} labels from {len(files)} report(s).\n" + digest.calibration(rows)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Job outreach agent (Phase 1)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "label"])
    parser.add_argument("files", nargs="*", type=Path, help="label: report files (default reports/*.md)")
    parser.add_argument("--dry-run", action="store_true", help="local report only (required in Phase 1)")
    parser.add_argument("--force", action="store_true", help="bypass the once-per-day guard (Phase 2)")
    parser.add_argument("--sources", default="hn,remoteok,wwr")
    parser.add_argument("--profile", type=Path, help="profile YAML (default candidate/profile.yaml)")
    parser.add_argument("--rescore", action="store_true",
                        help="re-score already evaluated fresh jobs (model comparison; uses a Claude call)")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    (ROOT / "logs").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / f"{now.astimezone(PKT):%Y-%m-%d}.log",
                                                      encoding="utf-8"), logging.StreamHandler()])
    conn = repo.connect(ROOT / "data" / "jobs.db")

    if args.command == "label":
        files = args.files or sorted((ROOT / "reports").glob("*.md"))
        print(label_command(conn, files))
        return 0
    if not args.dry_run:
        print("Phase 1 supports only --dry-run (no Gmail drafts yet).")
        return 2

    cfg, profile = load_config(), load_profile(path=args.profile)
    http = Http(cfg["http"], ROOT / "data" / "cache")
    local = now.astimezone(PKT)
    src = cfg["sources"]
    available = {
        "hn": lambda: hn_whos_hiring.fetch(http, local),
        "remoteok": lambda: remoteok.fetch(http, local),
        "wwr": lambda: wwr.fetch(http, local, src["wwr"]["feeds"]),
    }
    wanted = [s.strip() for s in args.sources.split(",")]
    fetchers = {n: f for n, f in available.items() if n in wanted and src.get(n, {}).get("enabled", True)}
    result = run_pipeline(conn, cfg, profile, now=now, fetchers=fetchers, http=http, rescore=args.rescore)
    print(json.dumps({k: v for k, v in result.items() if k != "report"}, indent=2, default=str))
    print(f"Report: {result['report']}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
