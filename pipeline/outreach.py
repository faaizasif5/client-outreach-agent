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
from pipeline import drafter, freshness, people as people_mod, resume_builder
from pipeline import stack as stack_mod
from pipeline.claude_cli import run_claude
from schemas.ai_output import FormAnswer

log = logging.getLogger(__name__)
PKT = timezone(timedelta(hours=5))
ROOT = Path(__file__).resolve().parent.parent
TYPE = {"email": "direct_email", "portal": "application_brief", "network": "network_pitch"}
CAP = {"email": "email_drafts", "portal": "portal_kits", "network": "linkedin_requests"}


def _today_count(conn, table: str, outreach_type: str, now: datetime) -> int:
    today = now.astimezone(PKT).date()
    rows = conn.execute(f"SELECT created_at FROM {table} WHERE outreach_type=?", (outreach_type,)).fetchall()
    return sum(1 for r in rows if repo.parse_iso(r["created_at"]).astimezone(PKT).date() == today)


def _cap(cfg, route) -> int:
    name = CAP[route]
    return min(cfg["caps"][name], cfg["hard_max"].get(name, cfg["caps"][name]))


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
    via = {"email": f" to {job.get('contact_email')}", "portal": f" via {job.get('apply_url')}",
           "network": " (LinkedIn connection note is the pitch)"}[meta["route"]]
    lines = [f"# {job['company']}: {job['title']}", "",
             f"- Route: {meta['route']}{via}",
             f"- Bucket: {job['bucket']}" + (f" (pitch variant {meta['pitch_variant']})" if meta["pitch_variant"] else ""),
             f"- Resume: {meta['resume_key']} -> {meta['resume_path']}"
             + (f" (attached as {meta['attachment']})" if meta["route"] == "email" else ""),
             f"- Fit: {meta['band']} {meta['fit']}", f"- Listing: {meta['listing_url']}"]
    tailored = meta.get("tailored")
    if tailored:
        lines.append(f"- Tailored resume: {tailored['pdf']}" if tailored["status"] == "used"
                     else f"- Tailored resume rejected ({'; '.join(tailored['problems'])}); standard PDF used")
    lines.append("")
    if meta["route"] == "email":
        lines += ["## Subject", "", draft.subject or "", "", "## Body", "", draft.body, ""]
    elif meta["route"] == "network":
        pass
    else:
        lines += ["## Cover note", "", draft.kit.cover_note, "", "## Form answers", ""]
        for a in draft.kit.form_answers:
            lines += [f"**{a.question}**", "", a.answer, ""]
        lines += ["## Standard fields (copy-paste)", "", "```", meta["standard_fields"], "```", ""]
    if draft.network:
        lines += [f"## LinkedIn (network: {meta.get('network_mode')})", ""]
        for p in meta.get("people") or []:
            lines.append(f"- {p['name']} ({p.get('title')}), {p.get('linkedin_url')} "
                         f"[{p['source']}, {'verified' if p.get('verified') else 'check'}]: connect without a note")
        lines += ["", "Message to send after they accept:", "", draft.network.after_accept_message, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _listing_url(job: dict) -> str | None:
    sources = json.loads(job.get("sources") or "[]")
    return sources[0]["url"] if sources else job.get("apply_url")


def pre_sync(conn, google: dict, cfg: dict, now: datetime, http=None, fetched_sources: set[str] = frozenset()) -> dict:
    """Live only: recover/reconcile Gmail, stale drafts (freshness #3), no_response, sheet sync,
    ATS confirmations, kit expiry, Network sync, queued rows."""
    out = {"errors": []}
    gmail, api, sid = google.get("gmail"), google.get("sheets"), google.get("sheet_id")

    def liveness(job):
        return freshness.check_liveness(job, http, cfg["eligibility"]["closed"], now=now,
                                        fetched_sources=set(fetched_sources),
                                        complete_feeds=set(cfg["outreach"]["complete_feeds"]))[0] if http else "open"
    steps = [
        ("gmail_recover", lambda: gmail_drafts.recover(conn, gmail, cfg, now)),
        ("gmail_reconcile", lambda: gmail_drafts.reconcile(conn, gmail, now)),
        ("stale_drafts", lambda: gmail_drafts.cleanup_stale(conn, gmail, cfg, now, liveness=liveness)),
        ("no_response", lambda: gmail_drafts.mark_no_response(conn, now, cfg["followup"]["no_response_days"])),
        ("sheet_sync", lambda: st.sync_from_sheet(conn, api, sid, now)),
        ("ats_confirmations", lambda: st.detect_confirmations(
            conn, gmail.recent_inbox_headers(), cfg["outreach"]["ats_confirmation"], now)),
        ("expired_kits", lambda: st.expire_pending(conn, api, sid, now, cfg["outreach"]["pending_kit_days"])),
        ("network_sync", lambda: st.sync_network(conn, api, sid, now)),
        ("archived", lambda: st.archive_rows(conn, api, sid, now) if cfg.get("sheets", {}).get("auto_archive", True)
         else {"archived": 0}),
        ("sheet_queue", lambda: st.flush_queue(conn, api, sid, now)),   # rows queued by an earlier run
    ]
    for name, step in steps:
        try:
            out[name] = step()
        except Exception as e:   # one failing integration never stops the run
            log.exception("%s failed", name)
            out["errors"].append(f"{name}: {type(e).__name__}: {e}"[:300])
    return out




def first_name(person: dict) -> str:
    name = (person.get("name") or "").strip()
    return "there" if not name or name.startswith("(") else name.split()[0]


def personalize(text: str | None, person: dict, everyone: list[dict]) -> str | None:
    """Fill {first_name} with this person's name. If Claude wrote a real name instead, swap any
    other person's first name for this one, so nobody gets a message addressed to someone else."""
    if not text:
        return text
    mine = first_name(person)
    text = text.replace("{first_name}", mine)
    for other in everyone:
        theirs = first_name(other)
        if theirs not in (mine, "there"):
            text = re.sub(rf"\b{re.escape(theirs)}\b", mine, text)
    return text


def _done(conn, item: dict, live: bool):
    """Existing outreach for this job: applications / network rows (live) or dry_run_drafts."""
    if not live:
        return conn.execute("SELECT * FROM dry_run_drafts WHERE job_key=?", (item["job_key"],)).fetchone()
    if item["route"] == "network":
        return conn.execute("SELECT * FROM network WHERE job_key=?", (item["job_key"],)).fetchone()
    return conn.execute("SELECT * FROM applications WHERE job_key=?", (item["job_key"],)).fetchone()


def _created_today(conn, route: str, live: bool, now: datetime) -> int:
    if not live:
        return _today_count(conn, "dry_run_drafts", TYPE[route], now)
    if route == "network":
        today = now.astimezone(PKT).date()
        rows = conn.execute("SELECT created_at FROM network WHERE is_pitch = 1").fetchall()
        return sum(1 for r in rows if repo.parse_iso(r["created_at"]).astimezone(PKT).date() == today)
    return _today_count(conn, "applications", TYPE[route], now)


def gather_people(conn, http, item: dict, now: datetime) -> list[dict]:
    """YC page people (saved by the YC scraper), the company's team page (fetched once), and
    names or profile links from the listing itself. Max 2, founders first."""
    if http:
        try:
            people_mod.fetch_team_page(conn, http, item["company_norm"], item.get("company_domain"), now)
        except Exception as e:
            log.info("team page lookup failed for %s: %s", item["company_norm"], e)
    listing = people_mod.from_listing(item.get("description") or "", json.loads(item.get("founder_links") or "[]"),
                                      item["company"])
    people_mod.save_people(conn, item["company_norm"], listing, now)
    return people_mod.people_for(conn, item["company_norm"])


def _network_sheet_rows(conn, job: dict, now: datetime, linked: str) -> None:
    for row in conn.execute("SELECT n.*, p.name, p.title, p.linkedin_url, p.source, p.verified FROM network n "
                            "JOIN people p ON p.id=n.person_id WHERE n.job_key=?", (job["job_key"],)).fetchall():
        st.enqueue(conn, st.NETWORK, row["id"], st.network_row(dict(row), job, linked), now)


def run_stage(conn, cfg: dict, profile: dict, items: list[dict], *, now: datetime, run_id: str, http,
              live: bool, google: dict | None = None, report_dir: Path, fetched_sources: set[str],
              claude=run_claude) -> dict:
    out = {"drafts_created": 0, "kits_created": 0, "pitches_created": 0, "created": [], "rejected": {},
           "closed": [], "unknown": [], "network_waiting": [], "errors": [], "usage": None, "existing": []}
    if live and google:
        out["sync"] = pre_sync(conn, google, cfg, now, http=http, fetched_sources=fetched_sources)
        out["errors"] += out["sync"].pop("errors")
        out["replies"] = (out["sync"].get("gmail_reconcile") or {}).get("replied", 0)

    remaining = {r: _cap(cfg, r) - _created_today(conn, r, live, now) for r in CAP}
    targets = []
    for item in (i for i in items if i["decision"] == "shortlisted"):
        done = _done(conn, item, live)
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
        if status == "gated":   # job board wants a sign-up/payment to apply: never pay, apply on the company site
            repo.update_job(conn, item["job_key"], {"eligibility": "uncertain", "eligibility_reason": "gated_apply_link"})
            out.setdefault("gated", []).append(item)
            continue
        found = gather_people(conn, http, item, now)
        if item["route"] == "network" and not found:
            out["network_waiting"].append(item)
            continue
        remaining[item["route"]] -= 1
        questions = []
        if item["route"] == "portal":
            questions = drafter.greenhouse_questions(http, item.get("apply_url")) if http else []
            questions = questions or list(cfg["drafting"]["default_questions"])
        tailor = (item["route"] != "network" and item["fit_score"] >= cfg["tailoring"]["min_fit"]
                  and resume_builder.available(item["resume_key"]))
        targets.append(dict(item, outreach_type=TYPE[item["route"]], form_questions=questions,
                            pitch_variant=None, people=found, tailor=tailor,
                            stack=stack_mod.match(f"{item['title']}\n{item.get('description') or ''}", profile, cfg)))

    # LinkedIn requests go out without a note (free-tier limit), so there is no note budget: each
    # row gets one message for after they accept.
    for t in targets:
        if not t["people"]:
            t["network_mode"] = "none"
        else:
            t["network_mode"] = {"email": "after_accept_only", "portal": "applied", "network": "pitch"}[t["route"]]
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
    counter = {"email": "drafts_created", "portal": "kits_created", "network": "pitches_created"}
    for n, (key, d) in enumerate(result["accepted"].items(), 1):
        job = by_key[key]
        if d.kit:   # work-authorization questions are answered by code, truthfully
            d.kit.form_answers += [FormAnswer(question=q, answer=profile["answer_bank"]["work_authorization"])
                                   for q in job.get("auth_questions", [])]
        tailored = None
        if d.tailored:   # spec 11a: any failed check -> the standard PDF is attached instead
            out_dir = (folder / "tailored" / f"{n:02d}-{_slug(job['company'])}" if not live
                       else ROOT / "applications" / _slug(key))
            tailored = resume_builder.build(key, job["resume_key"], d.tailored, profile, cfg, out_dir)
            if tailored["status"] == "used":
                job = dict(job, resume_path=tailored["pdf"])
            if live:
                conn.execute("INSERT OR REPLACE INTO tailored_resumes (job_key, resume_key, spec_json, pdf_path, "
                             "status, problems, created_at) VALUES (?,?,?,?,?,?,?)",
                             (key, job["resume_key"], d.tailored.model_dump_json(), tailored["pdf"],
                              tailored["status"], json.dumps(tailored["problems"]), repo.iso(now)))
            out.setdefault("tailored", []).append({"item": job, **tailored})
        meta = {"route": job["route"], "pitch_variant": job["pitch_variant"], "resume_key": job["resume_key"],
                "resume_path": job["resume_path"], "attachment": cfg["outreach"]["attachment_filename"],
                "band": job["band"], "fit": job["fit_score"], "listing_url": _listing_url(job),
                "standard_fields": standard, "people": job["people"], "network_mode": job["network_mode"],
                "tailored": tailored}
        if not live:
            path = write_dry_run_file(folder, n, job, d, meta)
            conn.execute("INSERT OR IGNORE INTO dry_run_drafts (job_key, run_id, outreach_type, payload, file_path, "
                         "created_at) VALUES (?,?,?,?,?,?)",
                         (key, run_id, job["outreach_type"], json.dumps({**d.model_dump(), **meta}, default=str),
                          str(path), repo.iso(now)))
            out["created"].append({"item": job, "file": str(path)})
            out[counter[job["route"]]] += 1
            continue
        linked = "email draft"
        if job["route"] == "email":
            status = gmail_drafts.create_email_draft(
                conn, google["gmail"], job=job, draft=d, resume_key=job["resume_key"],
                resume_path=job["resume_path"], pitch_variant=job["pitch_variant"], cfg=cfg, now=now)
            out["created"].append({"item": job, "status": status})
            out["drafts_created"] += status == "drafted"
            if status != "drafted":
                continue
        elif job["route"] == "portal":
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
                job, app, job["fit_score"], " · ".join(x for x in (job["reason"], job["stack"].summary()) if x),
                (google.get("resume_links") or {}).get(job["resume_key"]),
                "__KIT__"), now)
            out["created"].append({"item": job, "status": "pending"})
            out["kits_created"] += 1
            linked = "Applications tab"
        else:
            linked = "LinkedIn pitch"
        if d.network:
            created_any = False
            for person in job["people"]:
                nid = repo.create_network_entry(
                    conn, job_key=key, person_id=person["id"], now=now, is_pitch=job["route"] == "network",
                    after_accept_message=personalize(d.network.after_accept_message, person, job["people"]))
                created_any = created_any or bool(nid)
            if created_any:
                _network_sheet_rows(conn, job, now, linked)
                if job["route"] == "network":
                    out["created"].append({"item": job, "status": "to_connect"})
                    out["pitches_created"] += 1
    if live and google and google.get("sheets"):
        out["sheet"] = st.flush_queue(conn, google["sheets"], google["sheet_id"], now)
        if out["sheet"].get("error"):
            out["errors"].append(f"sheet write failed (queued): {out['sheet']['error']}")
    return out
