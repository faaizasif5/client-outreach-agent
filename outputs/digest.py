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
    out = [f"# Job report {ctx['date']} (dry run)", "",
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
