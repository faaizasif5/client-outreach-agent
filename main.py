"""Daily pipeline entry point.

    python main.py --dry-run          fetch, gate, score, draft -> reports/<date>.md + reports/dry-run/
    python main.py                    live: Gmail drafts + tracker sheet (needs outreach.live_enabled)
    python main.py label [files...]   read labels back from reports and print calibration
    python main.py setup-google       one-time OAuth + create the tracker sheet + upload resumes
    python main.py scheduler off      pause the daily Windows task (on | off | status)
"""
import argparse
import json
import logging
import os
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
from outputs import digest, notify
from pipeline import contacts as contacts_mod
from pipeline import dedupe, eligibility, freshness, keyword_match, normalize, outreach, people, schedule, scorer
from pipeline.claude_cli import build_command, run_claude

ROOT = Path(__file__).resolve().parent
PKT = timezone(timedelta(hours=5))   # Asia/Karachi has no DST; avoids a tzdata dependency on Windows
log = logging.getLogger("outreach_agent")


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
    if raw.source == "wwr":   # WWR's own apply button is gated: prefer the company's link in the listing
        direct = next((l for l in raw.links if contacts_mod.is_apply_url(l) and "weworkremotely.com" not in l), None)
        explicit_apply = direct or explicit_apply
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

    # Founder LinkedIn links found on the YC page or the company's own site also count as a
    # direct contact for stretch routing (Network tab pitch).
    founders = json.loads(row["founder_links"] or "[]") + people.verified_profile_links(conn, row["company_norm"])
    merged_contacts = contacts_mod.Contacts(
        recruiting_email=row["contact_email"], apply_url=row["apply_url"],
        founder_links=founders, preference=contacts.preference, flags=contacts.flags)
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
    if row["eligibility_reason"] not in ("hidden_location_restriction", "gated_apply_link"):   # sticky
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
    # keyword_filtered jobs are re-checked each run, so a keyword added to config.yaml takes effect.
    unscored = "" if rescore else (
        "AND status IN ('new','score_missing','keyword_filtered') AND job_key NOT IN (SELECT job_key FROM evaluations)")
    rows = conn.execute(
        "SELECT * FROM jobs WHERE eligibility IN ('eligible','stretch') AND lifecycle='open' "
        f"AND posted_at >= ? {unscored}", (cutoff,)).fetchall()
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
                 check_claude=claude_ready, rescore: bool = False, live: bool = False,
                 google: dict | None = None, draft_claude=None, source_info: dict | None = None) -> dict:
    """source_info: filled by fetchers that report health (YC): {'yc': {...notes, status...}}."""
    source_info = source_info if source_info is not None else {}
    local = now.astimezone(PKT)
    run_id = local.strftime("%Y%m%dT%H%M%S%z")
    conn.execute("INSERT INTO runs (run_id, started_at, status) VALUES (?,?, 'running')",
                 (run_id, repo.iso(now)))
    errors: list[str] = []
    counts: Counter = Counter()
    fetched: set[str] = set()
    for name, fetch in fetchers.items():
        try:
            raws = fetch()
        except Exception as e:   # one source failing never stops the others
            log.exception("source %s failed", name)
            errors.append(f"source {name}: {type(e).__name__}: {e}")
            continue
        fetched.add(name)
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
    log.info("claude scoring usage: %s", json.dumps(usage, default=str))
    usage = {"scoring": usage, "drafting": None}

    items = scorer.shortlist(conn, cfg, profile, now)
    stage = None
    if status == "success":
        stage = outreach.run_stage(conn, cfg, profile, items, now=now, run_id=run_id, http=http, live=live,
                                   google=google, report_dir=report_dir, fetched_sources=fetched,
                                   claude=draft_claude or claude)
        usage["drafting"] = stage["usage"]
        errors += stage["errors"]
        if any(e.startswith("drafting failed") for e in stage["errors"]):
            status = "failed"
        log.info("claude drafting usage: %s", json.dumps(stage["usage"], default=str))
    else:   # spec section 13: on a failed stage nothing downstream runs
        errors.append("drafting skipped because scoring failed")
    today = local.date().isoformat()
    todays = [i for i in items if i["decision"] in ("shortlisted", "queued") or
              repo.parse_iso(i["evaluated_at"]).astimezone(PKT).date().isoformat() == today]
    seen_now = conn.execute("SELECT * FROM jobs WHERE last_seen_at = ?", (repo.iso(now),)).fetchall()
    # Only engineering roles are worth a manual look; newest first.
    uncertain = [{"company": r["company"], "title": r["title"], "reason": r["eligibility_reason"],
                  "location_text": r["location_text"], "sources": r["sources"], "posted_at": r["posted_at"]}
                 for r in sorted(seen_now, key=lambda r: r["posted_at"] or "", reverse=True)
                 if r["eligibility"] == "uncertain" and keyword_match.is_engineering_role(r["title"], cfg)]
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
        "drafts_created": stage["drafts_created"] if stage else 0,
        "kits_created": stage["kits_created"] if stage else 0,
        "pitches_created": stage.get("pitches_created", 0) if stage else 0,
        "replies": (stage or {}).get("replies", 0),
    })
    notes = list((google or {}).get("notes", []))
    for info in source_info.values():
        notes += info.get("notes", [])
        if info.get("repair_usage"):
            usage["yc_repair"] = info["repair_usage"]
    counts["yc_manual_fix"] = int(any(i.get("manual_fix_needed") for i in source_info.values()))
    from outputs import stats
    weekly = stats.weekly(conn, now, cfg) if stats.is_stats_day(now, cfg) else []
    existing = {r["job_key"]: r["label"] for r in conn.execute("SELECT job_key, label FROM labels")}
    report_ctx = {
        "date": today, "run_id": run_id, "counts": dict(counts), "usage": usage, "errors": errors,
        "items": todays, "uncertain": uncertain, "ineligible_counts": ineligible,
        "existing_labels": existing, "outreach": stage, "live": live, "notes": notes, "weekly": weekly}
    report = digest.write_report(report_dir / f"{today}.md", report_ctx)            # full: labels, debugging
    digest_file = digest.write_digest(report_dir / f"{today}-digest.md", report_ctx)  # short: what needs you
    conn.execute("UPDATE runs SET finished_at=?, status=?, counts=?, usage=?, errors=? WHERE run_id=?",
                 (repo.iso(datetime.now(timezone.utc)), status, json.dumps(dict(counts)),
                  json.dumps(usage, default=str), json.dumps(errors), run_id))
    return {"run_id": run_id, "status": status, "counts": dict(counts), "usage": usage,
            "errors": errors, "report": report, "digest": digest_file, "digest_ctx": report_ctx, "outreach": stage}


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


# ---------------------------------------------------------------- Google (live mode)

def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def google_context(conn, profile: dict, interactive: bool = False) -> dict:
    """Gmail client + Sheets API + tracker sheet id. Recreates the sheet from SQLite if it is gone."""
    from outputs import gmail_client, sheets_tracker as st
    creds = gmail_client.load_credentials(ROOT / "credentials.json", ROOT / "token.json", interactive)
    services = gmail_client.build_services(creds)
    address = os.environ.get("GMAIL_ADDRESS") or profile.get("answer_bank", {}).get("email", "")
    api = st.SheetsApi(services["sheets"], services["drive"], gmail_client.execute)
    links = {r["resume_key"]: r["link"] for r in conn.execute("SELECT resume_key, link FROM drive_files")}
    ctx = {"gmail": gmail_client.GmailClient(services["gmail"], address), "sheets": api,
           "sheet_id": os.environ.get("TRACKER_SHEET_ID"), "resume_links": links, "notes": []}
    if not ctx["sheet_id"] or not api.exists(ctx["sheet_id"]):
        if ctx["sheet_id"]:
            ctx["notes"].append("Tracker sheet was missing; recreated it from SQLite.")
        ctx["sheet_id"], _ = api.create()
        set_env_value(ROOT / ".env", "TRACKER_SHEET_ID", ctx["sheet_id"])
        conn.execute("DELETE FROM sheet_queue")
        for tab, job_key, row in st.rebuild_rows(conn, links):
            st.enqueue(conn, tab, job_key, row, datetime.now(timezone.utc))
    else:
        try:   # widths, wrapping, 1-row freeze: one cheap call, keeps the sheet readable
            api.ensure_tab(ctx["sheet_id"], st.ARCHIVE)
            api.apply_layout(ctx["sheet_id"])
        except Exception as e:
            ctx["notes"].append(f"Sheet layout not applied: {type(e).__name__}")
    return ctx


def setup_google(conn, profile: dict) -> str:
    """One-time: OAuth in the browser, create the tracker, upload resume PDFs to Drive."""
    ctx = google_context(conn, profile, interactive=True)
    api, now = ctx["sheets"], datetime.now(timezone.utc)
    uploaded = []
    resumes = profile.get("resumes") or {}
    if not conn.execute("SELECT 1 FROM drive_files").fetchone():
        folder = api.create_folder("Job Tracker Resumes")
        for key, entry in resumes.items():
            path = ROOT / entry["file"] if isinstance(entry, dict) and entry.get("file") else None
            if path and path.is_file():
                file_id, link = api.upload_pdf(path, path.name, folder)
                conn.execute("INSERT OR REPLACE INTO drive_files (resume_key, file_id, link, uploaded_at) "
                             "VALUES (?,?,?,?)", (key, file_id, link, repo.iso(now)))
                uploaded.append(key)
    return (f"Google setup done. Tracker: https://docs.google.com/spreadsheets/d/{ctx['sheet_id']} "
            f"(id saved to .env). Resumes uploaded: {uploaded or 'already uploaded'}.")


def send_digest(conn, gmail, run_id: str, ctx: dict, now: datetime, delivery: str = "send") -> None:
    """Daily digest to yourself as an HTML email (plain-text fallback). delivery 'draft' keeps the
    spec's original behaviour: a draft that replaces today's earlier digest draft."""
    from outputs.gmail_client import build_mime
    subject = f"Job digest {now.astimezone(PKT):%Y-%m-%d}"
    msg = build_mime(sender=gmail.address, to=gmail.address, subject=subject, body=digest.render_digest(ctx),
                     html=digest.render_digest_html(ctx), job_key=f"digest:{run_id}")
    if delivery == "send":
        message_id = gmail.send(msg)
        conn.execute("UPDATE runs SET digest_draft_id=? WHERE run_id=?", (f"sent:{message_id}", run_id))
        return
    for r in schedule.runs_today(conn, now):
        old = conn.execute("SELECT digest_draft_id FROM runs WHERE run_id=?", (r["run_id"],)).fetchone()[0]
        if old and not old.startswith("sent:"):
            gmail.delete_draft(old)
            conn.execute("UPDATE runs SET digest_draft_id=NULL WHERE run_id=?", (r["run_id"],))
    draft_id, _ = gmail.create_draft(msg)
    conn.execute("UPDATE runs SET digest_draft_id=? WHERE run_id=?", (draft_id, run_id))


# ---------------------------------------------------------------- CLI

def scheduler_command(words: list) -> int:
    action = words[0] if words else "status"
    if action not in ("on", "off", "status"):
        print("usage: main.py scheduler on | off | status")
        return 2
    try:
        status = schedule.task_status() if action == "status" else schedule.set_task_enabled(action == "on")
    except RuntimeError as e:
        print(e)
        return 1
    print(f"{schedule.TASK_NAME}: {status}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Client outreach agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "label", "setup-google", "yc-login", "scheduler"])
    parser.add_argument("files", nargs="*", help="label: report files (default reports/*.md); "
                                                 "scheduler: on | off | status")
    parser.add_argument("--dry-run", action="store_true", help="drafts/kits to reports/dry-run/, no Google calls")
    parser.add_argument("--force", action="store_true", help="bypass the once-per-day guard")
    parser.add_argument("--sources", default="hn,remoteok,wwr,yc")
    parser.add_argument("--profile", type=Path, help="profile YAML (default candidate/profile.yaml)")
    parser.add_argument("--rescore", action="store_true",
                        help="re-score already evaluated fresh jobs (model comparison; uses a Claude call)")
    args = parser.parse_args(argv)
    if args.command == "scheduler":   # the Windows task only; manual runs keep working
        return scheduler_command(args.files)

    load_dotenv(ROOT / ".env")
    (ROOT / "logs").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / f"{now.astimezone(PKT):%Y-%m-%d}.log",
                                                      encoding="utf-8"), logging.StreamHandler()])
    conn = repo.connect(ROOT / "data" / "jobs.db")
    cfg = load_config()

    if args.command == "yc-login":   # you log in yourself; only the session is kept (data/yc_browser_profile)
        from fetchers import yc_scraper
        yc_scraper.interactive_login()
        print("Session saved." if yc_scraper.has_session() else "No session saved.")
        return 0
    if args.command == "label":
        files = [Path(f) for f in args.files] or sorted((ROOT / "reports").glob("*.md"))
        print(label_command(conn, files))
        return 0
    profile = load_profile(path=args.profile)
    if args.command == "setup-google":
        print(setup_google(conn, profile))
        return 0

    live = not args.dry_run
    if live and not cfg["outreach"]["live_enabled"]:
        print("Live mode is off (config.yaml outreach.live_enabled: false). Use --dry-run, or enable it "
              "after calibration.")
        return 2
    with schedule.run_lock(ROOT / "data" / "run.lock", cfg["schedule"]["stale_lock_minutes"]) as held:
        if not held:
            log.info("another run holds the lock; exiting")
            return 0
        if not args.force:   # once-per-day guard (spec section 19a), before anything else
            reason = schedule.guard_reason(conn, now, cfg["schedule"]["max_attempts_per_day"])
            if reason:
                log.info("skipping run: %s (use --force to override)", reason)
                return 0
            problem = None if schedule.internet_ok() else "no internet"
            problem = problem or claude_ready()
            if problem:
                log.info("skipping run: %s; will retry on the next trigger", problem)
                return 0
        return _run(conn, cfg, profile, args, now, live)


def _yc(conn, http, now, cfg, source_info: dict) -> list:
    from fetchers import yc_scraper
    raws, info = yc_scraper.fetch(conn, http, now, cfg=cfg)
    source_info["yc"] = info
    return raws


def _run(conn, cfg, profile, args, now, live) -> int:
    http = Http(cfg["http"], ROOT / "data" / "cache")
    local = now.astimezone(PKT)
    src = cfg["sources"]
    available = {
        "hn": lambda: hn_whos_hiring.fetch(http, local),
        "remoteok": lambda: remoteok.fetch(http, local),
        "wwr": lambda: wwr.fetch(http, local, src["wwr"]["feeds"]),
        "yc": lambda: _yc(conn, http, now, cfg, source_info),
    }
    source_info: dict = {}
    wanted = [s.strip() for s in args.sources.split(",")]
    fetchers = {n: f for n, f in available.items() if n in wanted and src.get(n, {}).get("enabled", True)}
    google = google_context(conn, profile) if live else None
    result = run_pipeline(conn, cfg, profile, now=now, fetchers=fetchers, http=http, rescore=args.rescore,
                          live=live, google=google, source_info=source_info)
    if live:
        try:
            links = {"drafts": notify.GMAIL_DRAFTS,
                     "tracker": f"https://docs.google.com/spreadsheets/d/{google['sheet_id']}" if google.get("sheet_id") else None}
            send_digest(conn, google["gmail"], result["run_id"], {**result["digest_ctx"], "links": links}, now,
                        delivery=cfg.get("digest", {}).get("delivery", "send"))
        except Exception as e:
            log.warning("digest draft failed: %s", e)
        counts = dict(result["counts"], failed_attempts=schedule.failed_attempts_today(conn, now),
                      error_type=(result["errors"][0].split(":")[0] if result["errors"] else None))
        sheet_url = f"https://docs.google.com/spreadsheets/d/{google['sheet_id']}" if google.get("sheet_id") else None
        notify.send(counts, topic=os.environ.get("NTFY_TOPIC"),
                    enabled=os.environ.get("NTFY_ENABLED", "true").lower() != "false",
                    server=cfg["notifications"]["ntfy_server"], sheet_url=sheet_url,
                    timeout=cfg["notifications"]["timeout_seconds"])
    printable = {k: v for k, v in result.items() if k not in ("report", "digest", "digest_ctx", "outreach")}
    print(json.dumps(printable, indent=2, default=str))
    print(f"Report: {result['report']}")
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
