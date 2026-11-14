"""Local markdown report (Phase 1) and the label read-back for score calibration."""
import json
import re
from collections import Counter
from pathlib import Path

LABELS = {"would_apply": "would_apply", "would_not": "would_not", "y": "would_apply",
          "yes": "would_apply", "apply": "would_apply", "n": "would_not", "no": "would_not",
          "skip": "would_not"}
LABEL_HEADER = ["#", "job_key", "Company", "Role", "Band", "Fit", "Conf", "Bucket", "Route",
                "Resume", "Decision", "Evidence", "label"]


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ").strip()


def _row(cells) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _link(text, url):
    return f"[{_cell(text)}]({url})" if url else _cell(text)


def _job_url(item) -> str | None:
    sources = json.loads(item.get("sources") or "[]")
    return sources[0]["url"] if sources else item.get("apply_url")


def _source_names(item) -> str:
    return ", ".join(sorted({s["source"] for s in json.loads(item.get("sources") or "[]")}))


def read_labels(markdown: str) -> dict[str, str]:
    """job_key -> normalized label, from any table that has job_key and label columns."""
    labels, header = {}, None
    for line in markdown.splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if "job_key" in header and "label" in header and len(cells) == len(header):
            key = cells[header.index("job_key")].strip("`")
            value = LABELS.get(cells[header.index("label")].lower())
            if key and value:
                labels[key] = value
    return labels


def _scored_table(items, existing_labels) -> list[str]:
    lines = [_row(LABEL_HEADER), _row(["---"] * len(LABEL_HEADER))]
    for n, i in enumerate(items, 1):
        resume = i["resume_key"] + ("" if i.get("resume_path") else " (no PDF)")
        if i.get("model_resume_key") and i["model_resume_key"] != i["resume_key"]:
            resume += f" (asked {i['model_resume_key']})"
        lines.append(_row([
            n, f"`{i['job_key']}`", i["company"], _link(i["title"], _job_url(i)), i["band"],
            i["fit_score"], i["confidence"], i["bucket"], i["route"], resume, i["decision"],
            "; ".join(i["evidence"]), existing_labels.get(i["job_key"], ""),
        ]))
    return lines


def usage_lines(usage) -> list[str]:
    """One line per Claude call: model, effort, tokens, time (full envelopes are in runs.usage)."""
    if not usage:
        return []
    stages = usage if "scoring" in usage else {"scoring": usage}
    lines = []
    for stage, u in stages.items():
        if not u:
            continue
        for call in u.get("calls", []):
            t = call.get("usage") or {}
            thinking = (t.get("output_tokens_details") or {}).get("thinking_tokens", 0)
            lines.append(f"- Claude {stage}: {u.get('model')} effort={u.get('effort')} "
                         f"input={t.get('input_tokens', 0) + t.get('cache_creation_input_tokens', 0) + t.get('cache_read_input_tokens', 0)} "
                         f"output={t.get('output_tokens')} (thinking {thinking}) time={call.get('duration_ms')}ms "
                         f"attempts={u.get('attempts')}")
    return lines


def outreach_section(stage: dict | None, live: bool) -> list[str]:
    if stage is None:
        return []
    where = "Gmail drafts / tracker sheet" if live else "dry run: files in reports/dry-run/, nothing sent to Google"
    out = [f"## Drafts and kits ({where})", ""]
    for c in stage.get("created", []):
        i = c["item"]
        kind = {"email": "email", "portal": "portal kit", "network": "LinkedIn pitch"}.get(i["route"], i["route"])
        target = c.get("file") or c.get("status")
        out.append(f"- {kind}: **{_cell(i['company'])}**, {_cell(i['title'])} ({i['band']} {i['fit_score']}, "
                   f"resume {i['resume_key']}{', variant ' + i['pitch_variant'] if i.get('pitch_variant') else ''}) -> {target}")
    for c in stage.get("existing", []):
        i = c["item"]
        out.append(f"- already drafted earlier: {_cell(i['company'])}, {_cell(i['title'])}"
                   + (f" -> {c['file']}" if c.get("file") else ""))
    if not stage.get("created") and not stage.get("existing"):
        out.append("_No new drafts or kits._")
    for key, problems in (stage.get("rejected") or {}).items():
        out.append(f"- rejected by guard `{key}`: {'; '.join(problems)}")
    for i, why in stage.get("closed", []):
        out.append(f"- closed at liveness check: {_cell(i['company'])}, {_cell(i['title'])} ({why})")
    for i, why in stage.get("unknown", []):
        out.append(f"- liveness unknown, retry next run: {_cell(i['company'])}, {_cell(i['title'])} ({why})")
    for i in stage.get("network_waiting", []):
        out.append(f"- LinkedIn pitch waiting (no founder found, or this month's note budget is used): "
                   f"{_cell(i['company'])}, {_cell(i['title'])}")
    for t in stage.get("tailored", []):
        i = t["item"]
        out.append(f"- tailored resume for {_cell(i['company'])}: " + (
            t["pdf"] if t["status"] == "used" else f"rejected ({'; '.join(t['problems'])[:200]}), standard PDF used"))
    sync = stage.get("sync") or {}
    if sync:
        out.append(f"- sync: {json.dumps({k: v for k, v in sync.items()}, default=str)[:600]}")
    if stage.get("sheet"):
        out.append(f"- sheet: {stage['sheet']}")
    return out + [""]


def render(ctx: dict) -> str:
    """ctx: date, run_id, counts, usage, errors, items (shortlist() output for today's
    evaluations), uncertain, suppressed, ineligible_counts, existing_labels."""
    labels = ctx.get("existing_labels", {})
    items = ctx["items"]
    top = [i for i in items if i["decision"] in ("shortlisted", "queued")]
    rest = [i for i in items if i["decision"] not in ("shortlisted", "queued")]
    out = [f"# Job report {ctx['date']}{'' if ctx.get('live') else ' (dry run)'}", "",
           f"Run `{ctx['run_id']}`. Sources: HN Who is Hiring, RemoteOK (remoteok.com), We Work Remotely.", ""]
    c = ctx["counts"]
    out += ["## Summary", "",
            ", ".join(f"{k}: {v}" for k, v in c.items()), ""]
    for line in usage_lines(ctx.get("usage")):
        out += [line]
    out += [""]
    if ctx.get("notes"):
        out += [f"> {n}" for n in ctx["notes"]] + [""]
    if ctx.get("errors"):
        out += ["## Errors", ""] + [f"- {e}" for e in ctx["errors"]] + [""]
    out += outreach_section(ctx.get("outreach"), ctx.get("live"))
    out += ctx.get("weekly") or []

    out += ["## Shortlist", "",
            "Ranked by band, then score. `queued` = over today's cap for that route; it waits while fresh.",
            "Fill the **label** column with `would_apply` or `would_not`, then run `python main.py label`.", ""]
    out += _scored_table(top, labels) if top else ["_No shortlisted jobs._"]
    out += [""]
    for i in top:
        flags = json.loads(i.get("flags") or "[]")
        notes = [f"posted {i['posted_at'][:10]} ({i.get('posted_at_source') or 'source'})"]
        if "date_estimated" in flags or i.get("posted_at_source") == "newest_first":
            notes.append("**date estimated**")
        notes += [f for f in flags if f != "date_estimated"]
        contact = i.get("contact_email") or i.get("apply_url") or ""
        out += [f"**{_cell(i['company'])}: {_cell(i['title'])}** ({i['band']} {i['fit_score']}, "
                f"{i['route']} via {contact}; sources: {_source_names(i)})",
                f"- Why: {_cell(i['reason'])}", f"- Notes: {', '.join(notes)}", ""]

    out += ["## Other scored jobs (label these too)", ""]
    out += _scored_table(rest, labels) if rest else ["_None._"]
    out += [""]

    out += ["## Uncertain (review manually, never drafted)", ""]
    if ctx["uncertain"]:
        out += [_row(["Company", "Role", "Reason", "Location"]), _row(["---"] * 4)]
        out += [_row([u["company"], _link(u["title"], _job_url(u)), u["reason"], u["location_text"]])
                for u in ctx["uncertain"]]
    else:
        out += ["_None._"]
    out += [""]

    out += ["## Second role at a blocked company", ""]
    sup = [i for i in items if i["decision"] == "suppressed_company"]
    out += [f"- {_cell(i['company'])}: {_cell(i['title'])} (blocked by {i.get('suppressed_by')})"
            for i in sup] or ["_None._"]
    out += ["", "## Ineligible today (by reason)", ""]
    out += [f"- {reason}: {n}" for reason, n in ctx["ineligible_counts"].most_common()] or ["_None._"]
    return "\n".join(out) + "\n"


def write_report(path: Path, ctx: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():   # keep labels typed into an earlier version of today's report
        ctx.setdefault("existing_labels", {}).update(read_labels(path.read_text(encoding="utf-8")))
    path.write_text(render(ctx), encoding="utf-8")
    return path


def calibration(rows: list[dict]) -> str:
    """rows: {label, fit_score}. Phase 1 exit criteria: >= ~80% of 80+ are would_apply,
    <= ~20% of <60 are would_apply."""
    bands = {"80+": lambda f: f >= 80, "60-79": lambda f: 60 <= f < 80, "<60": lambda f: f < 60}
    lines = [f"Labeled jobs: {len(rows)} (target ~50)", ""]
    for name, test in bands.items():
        in_band = [r for r in rows if test(r["fit_score"])]
        yes = sum(1 for r in in_band if r["label"] == "would_apply")
        pct = f"{100 * yes / len(in_band):.0f}%" if in_band else "n/a"
        target = {"80+": "target >= 80%", "<60": "target <= 20%"}.get(name, "")
        lines.append(f"  {name:>6}: {len(in_band):3d} labeled, would_apply {pct:>4} {target}")
    by_resume = Counter((r.get("resume_key"), r["label"]) for r in rows)
    if by_resume:
        lines += ["", "By resume: " + ", ".join(f"{k}/{l}={n}" for (k, l), n in sorted(by_resume.items(), key=str))]
    return "\n".join(lines)


# ---------------------------------------------------------------- concise daily digest (Gmail)

REASON_TEXT = {
    "region_unknown": "says remote, no region given",
    "age_unknown": "no posting date found",
    "conflicting_signals": "worldwide, but also asks for US authorization",
    "low_confidence_score": "listing too vague to score",
    "gated_apply_link": "job board wants a sign-up or payment to apply; apply on the company's own site",
}
SINCE = [  # (sync step, key, phrase)
    ("gmail_reconcile", "sent", "email(s) sent"), ("gmail_reconcile", "replied", "reply(ies) received"),
    ("gmail_reconcile", "discarded", "draft(s) deleted"), ("sheet_sync", "applied", "portal application(s) marked applied"),
    ("ats_confirmations", None, "applied via confirmation email"), ("sheet_sync", "discarded", "portal job(s) discarded"),
    ("network_sync", "requested", "LinkedIn request(s) sent"), ("network_sync", "connected", "LinkedIn connection(s) accepted"),
    ("stale_drafts", "expired", "unsent draft(s) expired"), ("expired_kits", None, "pending kit(s) expired"),
    ("no_response", None, "email(s) with no reply after 21 days"),
    ("archived", "archived", "finished row(s) moved to the Archive tab"),
]


def _short(text, n=140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _action_line(c: dict) -> str:
    i = c["item"]
    url = _job_url(i)
    head = f"- **{_cell(i['company'])}**, {_cell(i['title'])} ({i['band']} {i['fit_score']})"
    if i["route"] == "email":
        head += f" → to {i.get('contact_email')}"
    elif i["route"] == "network":
        who = [p["name"] for p in i.get("people") or []][:1]
        head += f" → connect with {who[0]}" if who else ""
    why = _short(i.get("reason"))
    sm = i.get("stack")
    stack_line = f"\n  {sm.summary()}" if sm and sm.summary() else ""
    return head + (f"\n  {why}" if why else "") + stack_line + (f" [listing]({url})" if url else "")


NOTHING_YET = re.compile(r"^Last 7 days: 0 emails sent, 0 drafts discarded, 0 portal applications, 0 drafts")


def weekly_lines(ctx: dict) -> list[str]:
    """The weekly stats as plain lines (no markdown); nothing at all in a week with no outreach."""
    lines = [re.sub(r"\*\*|^_|_$", "", l.lstrip("> ")) for l in ctx.get("weekly") or []
             if l and not l.startswith(("|", "## "))]
    lines = [l for l in lines if l not in ("No outreach yet.", "Discard rate: n/a")]
    return [] if all(NOTHING_YET.match(l) for l in lines) else lines


DECISION_TEXT = {
    "shortlisted": "shortlisted", "queued": "queued (over today's cap)", "below_threshold": "below threshold",
    "low_confidence": "low confidence", "skipped_by_ai": "skipped by AI", "not_eligible": "not eligible",
    "suppressed_company": "company already contacted",
}


def _split_items(ctx: dict) -> tuple[list, list, list]:
    """Today's scored jobs: shortlist, everything else, second roles at an already-contacted company."""
    items = ctx.get("items") or []
    top = [i for i in items if i.get("decision") in ("shortlisted", "queued")]
    blocked = [i for i in items if i.get("decision") == "suppressed_company"]
    rest = [i for i in items if i not in top and i not in blocked]
    return top, rest, blocked


def _job_notes(i: dict) -> str:
    flags = json.loads(i.get("flags") or "[]")
    notes = [f"posted {i['posted_at'][:10]}"] if i.get("posted_at") else []
    if "date_estimated" in flags or i.get("posted_at_source") == "newest_first":
        notes.append("date estimated")
    return ", ".join(notes + [f for f in flags if f != "date_estimated"])


def _rejected_lines(ctx: dict) -> list[str]:
    """Each draft the truthfulness checks rejected, with the company and why."""
    names = {i["job_key"]: f"{i['company']}, {i['title']}" for i in ctx.get("items") or [] if i.get("job_key")}
    return [f"Draft rejected for {names.get(key, key)}: {'; '.join(problems)}"
            for key, problems in ((ctx.get("outreach") or {}).get("rejected") or {}).items()]


def _run_lines(ctx: dict) -> list[str]:
    lines = []
    if ctx.get("ineligible_counts"):
        lines.append("Ineligible today: " + ", ".join(
            f"{r} {n}" for r, n in Counter(ctx["ineligible_counts"]).most_common()))
    return lines + [l[2:] for l in usage_lines(ctx.get("usage"))]


def render_digest(ctx: dict) -> str:
    """What needs the user today, then the full report: every scored job, every uncertain job, run
    details. reports/<date>.md is still written, only for labeling (`main.py label`)."""
    stage = ctx.get("outreach") or {}
    created = [c for c in stage.get("created", []) if c.get("status") not in ("suppressed", "exists", "draft_failed")]
    by_route = {r: [c for c in created if c["item"]["route"] == r] for r in ("email", "portal", "network")}
    where = "" if ctx.get("live") else " (dry run: see reports/dry-run/)"
    parts = [f"{len(by_route['email'])} email(s) to review and send in Gmail Drafts",
             f"{len(by_route['portal'])} portal application(s) in the tracker",
             f"{len(by_route['network'])} LinkedIn request(s) in the Network tab"]
    total = sum(len(v) for v in by_route.values())
    out = [f"# Job digest {ctx['date']}", "",
           ("Today: " + ", ".join(p for p, n in zip(parts, by_route.values()) if n) + where + ".") if total
           else "Nothing new to send today.", ""]
    tracker = (ctx.get("links") or {}).get("tracker")
    if tracker:
        out += [f"Tracker sheet: {tracker}", ""]

    titles = {"email": "Emails to send", "portal": "Portal applications", "network": "LinkedIn requests"}
    for route, items in by_route.items():
        if items:
            out += [f"## {titles[route]}", ""] + [_action_line(c) for c in items] + [""]

    sync = stage.get("sync") or {}
    since = []
    for step, key, phrase in SINCE:
        value = sync.get(step)
        n = (value or {}).get(key, 0) if key else (value or 0)
        if isinstance(n, int) and n:
            since.append(f"{n} {phrase}")
    if since:
        out += ["## Since last run", "", "- " + "; ".join(since), ""]

    attention = list(ctx.get("errors") or []) + list(ctx.get("notes") or [])
    failed = [c for c in stage.get("created", []) if c.get("status") == "draft_failed"]
    if failed:
        attention.append(f"{len(failed)} Gmail draft(s) failed; they retry next run.")
    attention = [_short(a, 220) for a in attention] + _rejected_lines(ctx)
    if attention:
        out += ["## Needs attention", ""] + [f"- {a}" for a in attention] + [""]

    uncertain = ctx.get("uncertain") or []
    if uncertain:
        out += ["## Worth a manual look", ""]
        for u in uncertain:
            url = _job_url(u)
            loc = f" ({_short(u['location_text'], 80)})" if u.get("location_text") else ""
            out.append(f"- {_cell(u['company'])}, {_cell(u['title'])}: {REASON_TEXT.get(u['reason'], u['reason'])}{loc}"
                       + (f" [listing]({url})" if url else ""))
        out.append("")

    top, rest, blocked = _split_items(ctx)
    for title, group in (("Shortlist", top), ("Other scored jobs", rest)):
        if group:
            out += [f"## {title}", ""]
            for i in group:
                url = _job_url(i)
                out.append(f"- **{_cell(i['company'])}**, {_cell(i['title'])} ({i['band']} {i['fit_score']}, "
                           f"{DECISION_TEXT.get(i['decision'], i['decision'])}, {i['route']})"
                           + (f" [listing]({url})" if url else ""))
                for extra in (_short(i.get("reason"), 220), _short("; ".join(i.get("evidence") or []), 300),
                              _job_notes(i)):
                    if extra:
                        out.append(f"  {extra}")
            out.append("")
    if blocked:
        out += ["## Second role at a blocked company", ""]
        out += [f"- {_cell(i['company'])}, {_cell(i['title'])} (blocked by {i.get('suppressed_by')})" for i in blocked]
        out.append("")

    weekly = weekly_lines(ctx)
    if weekly:
        out += ["## This week", ""] + [f"- {l}" for l in weekly] + [""]
    run = _run_lines(ctx)
    if run:
        out += ["## Run details", ""] + [f"- {l}" for l in run] + [""]
    return "\n".join(out) + "\n"


def write_digest(path: Path, ctx: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_digest(ctx), encoding="utf-8")
    return path


# ---------------------------------------------------------------- HTML email version

def _e(text) -> str:
    import html as htmllib
    return htmllib.escape(str(text or ""), quote=True)


BAND_STYLE = {"strong": ("#e6f4ea", "#137333"), "possible": ("#fef7e0", "#b06000"), "skip": ("#f1f3f4", "#5f6368")}
ROUTE_LABEL = {"email": "Email", "portal": "Portal", "network": "LinkedIn"}
FONT = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def _button(label: str, url: str | None) -> str:
    if not url:
        return ""
    return (f'<a href="{_e(url)}" style="display:inline-block;margin:6px 6px 0 0;padding:6px 12px;border-radius:6px;'
            f'background:#1a73e8;color:#ffffff;text-decoration:none;font-size:13px">{_e(label)}</a>')


def _card(c: dict, links: dict) -> str:
    i = c["item"]
    bg, fg = BAND_STYLE.get(i["band"], BAND_STYLE["skip"])
    badge = (f'<span style="background:{bg};color:{fg};border-radius:10px;padding:2px 8px;font-size:12px;'
             f'font-weight:600">{_e(i["band"])} {_e(i["fit_score"])}</span>')
    if i["route"] == "email":
        detail = f'To {_e(i.get("contact_email"))}'
        buttons = _button("Open Gmail Drafts", links.get("drafts"))
    elif i["route"] == "portal":
        detail = "Apply on the company's site with the prepared kit"
        buttons = _button("Open tracker", links.get("tracker"))
    else:
        person = (i.get("people") or [{}])[0]
        detail = f'Connect with {_e(person.get("name"))}' if person.get("name") else "Connect on LinkedIn"
        buttons = _button("Founder's LinkedIn", person.get("linkedin_url")) + _button("Open tracker", links.get("tracker"))
    buttons += _button("Listing", _job_url(i))
    return (f'<tr><td style="padding:12px 14px;border:1px solid #e0e0e0;border-radius:8px;background:#ffffff">'
            f'<div style="font-size:15px;font-weight:600;color:#202124">{_e(i["company"])} {badge}</div>'
            f'<div style="font-size:14px;color:#3c4043;margin-top:2px">{_e(i["title"])}</div>'
            f'<div style="font-size:13px;color:#5f6368;margin-top:6px">{detail}</div>'
            f'<div style="font-size:13px;color:#5f6368;margin-top:4px">{_e(_short(i.get("reason"), 180))}</div>'
            + (f'<div style="font-size:12px;color:#137333;margin-top:4px">{_e(i["stack"].summary())}</div>'
               if i.get("stack") and i["stack"].summary() else "") +
            f'<div>{buttons}</div></td></tr><tr><td style="height:8px"></td></tr>')


def _section(title: str, body: str) -> str:
    return (f'<tr><td style="padding:16px 0 6px;font-size:13px;font-weight:700;color:#5f6368;letter-spacing:.04em;'
            f'text-transform:uppercase">{_e(title)}</td></tr>{body}')


def _bullets(lines: list[str]) -> str:
    items = "".join(f'<li style="margin:3px 0">{l}</li>' for l in lines)
    return f'<tr><td><ul style="margin:0;padding-left:18px;font-size:14px;color:#3c4043">{items}</ul></td></tr>'


def _a(text, url) -> str:
    if not url:
        return _e(text)
    return f'<a href="{_e(url)}" style="color:#1a73e8;text-decoration:none">{_e(text)}</a>'


def _badge(i: dict) -> str:
    bg, fg = BAND_STYLE.get(i["band"], BAND_STYLE["skip"])
    return (f'<span style="background:{bg};color:{fg};border-radius:10px;padding:2px 8px;font-size:12px;'
            f'font-weight:600">{_e(i["band"])} {_e(i["fit_score"])}</span>')


def _scored_card(i: dict) -> str:
    meta = " · ".join(x for x in (DECISION_TEXT.get(i["decision"], i["decision"]),
                                  ROUTE_LABEL.get(i["route"], i["route"]), _job_notes(i)) if x)
    evidence = "; ".join(i.get("evidence") or [])
    return (f'<tr><td style="padding:12px 14px;border:1px solid #e0e0e0;border-radius:8px;background:#ffffff">'
            f'<div style="font-size:15px;font-weight:600;color:#202124">{_e(i["company"])} {_badge(i)}</div>'
            f'<div style="font-size:14px;margin-top:2px">{_a(i["title"], _job_url(i))}</div>'
            f'<div style="font-size:12px;color:#5f6368;margin-top:4px">{_e(meta)}</div>'
            f'<div style="font-size:13px;color:#3c4043;margin-top:6px">{_e(_short(i.get("reason"), 220))}</div>'
            + (f'<div style="font-size:12px;color:#5f6368;margin-top:4px">{_e(_short(evidence, 300))}</div>'
               if evidence else "")
            + '</td></tr><tr><td style="height:8px"></td></tr>')


def _scored_line(i: dict) -> str:
    evidence = "; ".join(i.get("evidence") or [])
    return (f'{_a(i["company"] + ", " + i["title"], _job_url(i))} {_badge(i)} '
            f'<span style="color:#5f6368">· {_e(DECISION_TEXT.get(i["decision"], i["decision"]))}</span>'
            + (f'<div style="font-size:12px;color:#5f6368;margin:2px 0 6px">{_e(_short(evidence, 240))}</div>'
               if evidence else ""))


def render_digest_html(ctx: dict) -> str:
    stage = ctx.get("outreach") or {}
    links = ctx.get("links") or {}
    created = [c for c in stage.get("created", []) if c.get("status") not in ("suppressed", "exists", "draft_failed")]
    by_route = {r: [c for c in created if c["item"]["route"] == r] for r in ("email", "portal", "network")}
    stats = "".join(
        f'<td style="padding:10px;text-align:center;background:#f8f9fa;border-radius:8px">'
        f'<div style="font-size:22px;font-weight:700;color:#202124">{len(by_route[r])}</div>'
        f'<div style="font-size:12px;color:#5f6368">{label}</div></td><td style="width:8px"></td>'
        for r, label in (("email", "emails to send"), ("portal", "portal applications"), ("network", "LinkedIn requests")))
    rows = [f'<tr><td style="font-size:20px;font-weight:700;color:#202124;padding-bottom:4px">Job digest</td></tr>',
            f'<tr><td style="font-size:13px;color:#5f6368;padding-bottom:12px">{_e(ctx["date"])}'
            f'{"" if ctx.get("live") else " · dry run"}</td></tr>',
            f'<tr><td><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{stats}</tr></table></td></tr>']
    if links.get("tracker"):   # every digest, even on a quiet day
        rows.append(f'<tr><td style="padding-top:6px">{_button("Open tracker sheet", links["tracker"])}</td></tr>')
    if not created:
        rows.append('<tr><td style="padding:14px 0;font-size:14px;color:#3c4043">Nothing new to send today.</td></tr>')
    titles = {"email": "Emails to send", "portal": "Portal applications", "network": "LinkedIn requests"}
    for route, items in by_route.items():
        if items:
            rows.append(_section(titles[route], "".join(_card(c, links) for c in items)))

    sync = stage.get("sync") or {}
    since = []
    for step, key, phrase in SINCE:
        value = sync.get(step)
        n = (value or {}).get(key, 0) if key else (value or 0)
        if isinstance(n, int) and n:
            since.append(f"{n} {_e(phrase)}")
    if since:
        rows.append(_section("Since last run", _bullets(since)))
    attention = [_e(_short(a, 220)) for a in list(ctx.get("errors") or []) + list(ctx.get("notes") or [])]
    attention += [_e(l) for l in _rejected_lines(ctx)]
    if attention:
        rows.append(_section("Needs attention", _bullets(attention)))
    uncertain = ctx.get("uncertain") or []
    if uncertain:
        lines = [f'{_a(u["company"] + ", " + u["title"], _job_url(u))} <span style="color:#5f6368">· '
                 f'{_e(REASON_TEXT.get(u["reason"], u["reason"]))}'
                 + (f' · {_e(_short(u["location_text"], 80))}' if u.get("location_text") else "") + '</span>'
                 for u in uncertain]
        rows.append(_section("Worth a manual look", _bullets(lines)))
    top, rest, blocked = _split_items(ctx)
    if top:
        rows.append(_section("Shortlist", "".join(_scored_card(i) for i in top)))
    if rest:
        rows.append(_section("Other scored jobs", _bullets([_scored_line(i) for i in rest])))
    if blocked:
        rows.append(_section("Second role at a blocked company", _bullets(
            [f'{_e(i["company"])}, {_e(i["title"])} <span style="color:#5f6368">· blocked by '
             f'{_e(i.get("suppressed_by"))}</span>' for i in blocked])))
    weekly = weekly_lines(ctx)
    if weekly:
        rows.append(_section("This week", _bullets([_e(l) for l in weekly])))
    run = _run_lines(ctx)
    if run:
        rows.append(_section("Run details", _bullets([f'<span style="color:#5f6368">{_e(l)}</span>' for l in run])))
    return (f'<!doctype html><html><body style="margin:0;padding:0;background:#f1f3f4">'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f3f4;{FONT}">'
            f'<tr><td align="center" style="padding:20px 12px">'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;'
            f'background:#ffffff;border-radius:12px"><tr><td style="padding:20px">'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(rows)}</table>'
            f'</td></tr></table>'
            f'</td></tr></table></body></html>')
