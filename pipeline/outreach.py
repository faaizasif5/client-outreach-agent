"""Phase 2 stage after the shortlist: liveness check -> Claude call #2 -> Gmail drafts / sheet kits.

Dry run (or outreach.live_enabled false): drafts and kits go to reports/dry-run/<date>/ and the
dry_run_drafts table, never to applications, so dry runs never block a company.
Live: applications rows (company suppression in the same transaction), Gmail drafts, sheet rows."""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import repo
from outputs import drafts as gmail_drafts
from outputs import sheets_tracker as st
from pipeline import drafter, freshness
from pipeline.claude_cli import run_claude
from schemas.ai_output import FormAnswer

log = logging.getLogger(__name__)
PKT = timezone(timedelta(hours=5))
TYPE = {"email": "direct_email", "portal": "application_brief"}
CAP = {"email": "email_drafts", "portal": "portal_kits"}


def _today_count(conn, table: str, outreach_type: str, now: datetime) -> int:
    today = now.astimezone(PKT).date()
    rows = conn.execute(f"SELECT created_at FROM {table} WHERE outreach_type=?", (outreach_type,)).fetchall()
    return sum(1 for r in rows if repo.parse_iso(r["created_at"]).astimezone(PKT).date() == today)


def _cap(cfg, route) -> int:
    name = CAP[route]
    return min(cfg["caps"][name], cfg["hard_max"][name])


def next_variant(conn) -> str:
    """Alternate a/b across all stretch pitches, live and dry-run."""
    n = conn.execute("SELECT COUNT(*) FROM applications WHERE pitch_variant IS NOT NULL").fetchone()[0]
    n += sum(1 for r in conn.execute("SELECT payload FROM dry_run_drafts").fetchall()
             if json.loads(r["payload"]).get("pitch_variant"))
    return "a" if n % 2 == 0 else "b"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def write_dry_run_file(folder: Path, n: int, job: dict, draft, meta: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{n:02d}-{_slug(job['company'])}-{_slug(job['title'])}.md"
    lines = [f"# {job['company']}: {job['title']}", "",
             f"- Route: {meta['route']}" + (f" to {job['contact_email']}" if meta["route"] == "email" else
                                             f" via {job.get('apply_url')}"),
             f"- Bucket: {job['bucket']}" + (f" (pitch variant {meta['pitch_variant']})" if meta["pitch_variant"] else ""),
             f"- Resume: {meta['resume_key']} -> {meta['resume_path']}"
             + (f" (attached as {meta['attachment']})" if meta["route"] == "email" else ""),
             f"- Fit: {meta['band']} {meta['fit']}", f"- Listing: {meta['listing_url']}", ""]
    if meta["route"] == "email":
        lines += ["## Subject", "", draft.subject or "", "", "## Body", "", draft.body, ""]
    else:
        lines += ["## Cover note", "", draft.kit.cover_note, "", "## Form answers", ""]
        for a in draft.kit.form_answers:
            lines += [f"**{a.question}**", "", a.answer, ""]
        lines += ["## Standard fields (copy-paste)", "", "```", meta["standard_fields"], "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _listing_url(job: dict) -> str | None:
    sources = json.loads(job.get("sources") or "[]")
    return sources[0]["url"] if sources else job.get("apply_url")


def pre_sync(conn, google: dict, cfg: dict, now: datetime) -> dict:
    """Live only: recover/reconcile Gmail, sync the sheet, ATS confirmations, expiry, queued rows."""
    out = {"errors": []}
    gmail, api, sid = google.get("gmail"), google.get("sheets"), google.get("sheet_id")
    steps = [
        ("gmail_recover", lambda: gmail_drafts.recover(conn, gmail, cfg, now)),
        ("gmail_reconcile", lambda: gmail_drafts.reconcile(conn, gmail, now)),
        ("sheet_sync", lambda: st.sync_from_sheet(conn, api, sid, now)),
        ("ats_confirmations", lambda: st.detect_confirmations(
            conn, gmail.recent_inbox_headers(), cfg["outreach"]["ats_confirmation"], now)),
        ("expired_kits", lambda: st.expire_pending(conn, api, sid, now, cfg["outreach"]["pending_kit_days"])),
        ("sheet_queue", lambda: st.flush_queue(conn, api, sid, now)),   # rows queued by an earlier run
    ]
    for name, step in steps:
        try:
            out[name] = step()
        except Exception as e:   # one failing integration never stops the run
            log.exception("%s failed", name)
            out["errors"].append(f"{name}: {type(e).__name__}: {e}"[:300])
    return out


def run_stage(conn, cfg: dict, profile: dict, items: list[dict], *, now: datetime, run_id: str, http,
              live: bool, google: dict | None = None, report_dir: Path, fetched_sources: set[str],
              claude=run_claude) -> dict:
    out = {"drafts_created": 0, "kits_created": 0, "created": [], "rejected": {}, "closed": [], "unknown": [],
           "network_waiting": [], "errors": [], "usage": None, "existing": []}
    if live and google:
        out["sync"] = pre_sync(conn, google, cfg, now)
        out["errors"] += out["sync"].pop("errors")
        out["replies"] = (out["sync"].get("gmail_reconcile") or {}).get("replied", 0)

    table = "applications" if live else "dry_run_drafts"
    remaining = {r: _cap(cfg, r) - _today_count(conn, table, TYPE[r], now) for r in CAP}
    targets = []
    for item in (i for i in items if i["decision"] == "shortlisted"):
        if item["route"] == "network":
            out["network_waiting"].append(item)     # Network tab is Phase 3
            continue
        done = conn.execute(f"SELECT * FROM {table} WHERE job_key=?", (item["job_key"],)).fetchone()
        if done:
            out["existing"].append({"item": item, "file": done["file_path"] if not live else None})
            continue
        if remaining[item["route"]] <= 0:
            continue
        if repo.company_block_reason(conn, item["company_norm"], item["company_domain"]):
            continue
        # Freshness check #2: liveness right before drafting
        status, why = freshness.check_liveness(
            item, http, cfg["eligibility"]["closed"], now=now, fetched_sources=fetched_sources,
            complete_feeds=set(cfg["outreach"]["complete_feeds"])) if http else ("open", "no_http")
        if status == "closed":
            repo.update_job(conn, item["job_key"], {"lifecycle": "closed", "eligibility": "ineligible",
                                                    "eligibility_reason": "closed"})
            out["closed"].append((item, why))
            continue
        if status == "unknown":
            out["unknown"].append((item, why))
            continue
        remaining[item["route"]] -= 1
        questions = []
        if item["route"] == "portal":
            questions = drafter.greenhouse_questions(http, item.get("apply_url")) if http else []
            questions = questions or list(cfg["drafting"]["default_questions"])
        targets.append(dict(item, outreach_type=TYPE[item["route"]], form_questions=questions,
                            pitch_variant=None))
    variant = next_variant(conn)
    for t in targets:   # stretch pitches alternate a/b (price-mention test)
        if t["bucket"] != "eligible":
            t["pitch_variant"] = variant
            variant = "b" if variant == "a" else "a"
    if not targets:
        return out

    result = drafter.draft_jobs(targets, profile, cfg, claude=claude)
    out["usage"] = result.get("usage")
    out["rejected"] = result["rejected"]
    if result.get("error"):
        out["errors"].append(f"drafting failed: {result['error']}")
    if result.get("missing"):
        out["errors"].append(f"no draft returned for: {result['missing']}")
    folder = report_dir / "dry-run" / now.astimezone(PKT).date().isoformat()
    by_key = {t["job_key"]: t for t in targets}
    standard = drafter.standard_fields(profile)
    for n, (key, d) in enumerate(result["accepted"].items(), 1):
        job = by_key[key]
        if d.kit:   # work-authorization questions are answered by code, truthfully
            d.kit.form_answers += [FormAnswer(question=q, answer=profile["answer_bank"]["work_authorization"])
                                   for q in job.get("auth_questions", [])]
        meta = {"route": job["route"], "pitch_variant": job["pitch_variant"], "resume_key": job["resume_key"],
                "resume_path": job["resume_path"], "attachment": cfg["outreach"]["attachment_filename"],
                "band": job["band"], "fit": job["fit_score"], "listing_url": _listing_url(job),
                "standard_fields": standard}
        if not live:
            path = write_dry_run_file(folder, n, job, d, meta)
            conn.execute("INSERT OR IGNORE INTO dry_run_drafts (job_key, run_id, outreach_type, payload, file_path, "
                         "created_at) VALUES (?,?,?,?,?,?)",
                         (key, run_id, job["outreach_type"], json.dumps({**d.model_dump(), **meta}), str(path),
                          repo.iso(now)))
            out["created"].append({"item": job, "file": str(path)})
            out["drafts_created" if job["route"] == "email" else "kits_created"] += 1
            continue
        if job["route"] == "email":
            status = gmail_drafts.create_email_draft(
                conn, google["gmail"], job=job, draft=d, resume_key=job["resume_key"],
                resume_path=job["resume_path"], pitch_variant=job["pitch_variant"], cfg=cfg, now=now)
            out["created"].append({"item": job, "status": status})
            out["drafts_created"] += status == "drafted"
        else:
            app_id = repo.create_application(conn, job_key=key, outreach_type="application_brief",
                                             bucket=job["bucket"], resume_key=job["resume_key"], now=now,
                                             resume_pdf_path=job["resume_path"])
            if app_id is None:
                continue
            payload = {**d.model_dump(), "standard_fields": standard}
            conn.execute("UPDATE applications SET draft_json=? WHERE id=?", (json.dumps(payload), app_id))
            app = dict(conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())
            st.enqueue(conn, st.KITS, key, st.kit_row(job, payload["kit"], standard), now)
            st.enqueue(conn, st.APPS, key, st.application_row(
                job, app, job["fit_score"], job["reason"], (google.get("resume_links") or {}).get(job["resume_key"]),
                "__KIT__"), now)
            out["created"].append({"item": job, "status": "pending"})
            out["kits_created"] += 1
    if live and google and google.get("sheets"):
        out["sheet"] = st.flush_queue(conn, google["sheets"], google["sheet_id"], now)
        if out["sheet"].get("error"):
            out["errors"].append(f"sheet write failed (queued): {out['sheet']['error']}")
    return out
