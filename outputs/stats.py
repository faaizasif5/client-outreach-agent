"""Weekly stats for the Monday digest (spec sections 9, 20 and 22)."""
import json
from collections import defaultdict
from datetime import datetime, timedelta

from db import repo

CONTACTED = {"sent", "replied", "interview", "offer", "rejected", "no_response", "applied"}
POSITIVE = {"replied", "interview", "offer"}


def _role_type(row) -> str:
    title = (row["title"] or "").lower()
    return "fde" if row["resume_key"] == "fde" or "forward deployed" in title else "other"


def _rows(conn):
    return conn.execute(
        "SELECT a.*, j.title, j.sources, (SELECT fit_score FROM evaluations e WHERE e.job_key=a.job_key "
        "ORDER BY evaluated_at DESC LIMIT 1) AS fit FROM applications a JOIN jobs j ON j.job_key=a.job_key"
    ).fetchall()


def _band(fit) -> str:
    if fit is None:
        return "unknown"
    return "80+" if fit >= 80 else "60-79" if fit >= 60 else "<60"


def breakdown(conn) -> dict[str, dict[str, dict[str, int]]]:
    """dimension -> value -> {contacted, positive, interviews}, all time."""
    dims = defaultdict(lambda: defaultdict(lambda: {"contacted": 0, "positive": 0, "interviews": 0}))
    for r in _rows(conn):
        if r["status"] not in CONTACTED and r["outcome"] not in POSITIVE:
            continue
        sources = json.loads(r["sources"] or "[]")
        values = {
            "source": sources[0]["source"] if sources else "?",
            "resume": r["resume_key"], "outreach": r["outreach_type"], "bucket": r["bucket"],
            "role type": _role_type(r), "band": _band(r["fit"]),
        }
        if r["pitch_variant"]:
            values["pitch variant"] = r["pitch_variant"]
        positive = r["status"] in POSITIVE or r["outcome"] in POSITIVE
        interview = r["status"] in ("interview", "offer") or r["outcome"] in ("interview", "offer")
        for dim, value in values.items():
            cell = dims[dim][value]
            cell["contacted"] += 1
            cell["positive"] += positive
            cell["interviews"] += interview
    return dims


def weekly(conn, now: datetime, cfg: dict) -> list[str]:
    week_ago = repo.iso(now - timedelta(days=7))
    history = conn.execute("SELECT h.new_status, h.ref, a.outreach_type, a.sent_at FROM status_history h "
                           "JOIN applications a ON CAST(h.ref AS INTEGER)=a.id WHERE h.kind='application' "
                           "AND h.changed_at >= ?", (week_ago,)).fetchall()
    expired_unreviewed = sum(1 for h in history if h["new_status"] == "expired" and h["outreach_type"] == "direct_email"
                             and not h["sent_at"])
    sent = sum(1 for h in history if h["new_status"] == "sent")
    discarded = sum(1 for h in history if h["new_status"] == "discarded" and h["outreach_type"] == "direct_email")
    applied = sum(1 for h in history if h["new_status"] == "applied")
    out = ["## Weekly stats", "",
           f"Last 7 days: {sent} emails sent, {discarded} drafts discarded, {applied} portal applications, "
           f"{expired_unreviewed} drafts expired unreviewed.",
           f"Discard rate: {100 * discarded / (sent + discarded):.0f}% (target < 20%)" if sent + discarded else
           "Discard rate: n/a", ""]
    dims = breakdown(conn)
    if not dims:
        return out + ["_No outreach yet._", ""]
    out += ["| Dimension | Value | Contacted | Positive replies | Rate | Interviews |", "| --- | --- | --- | --- | --- | --- |"]
    for dim in ("source", "resume", "outreach", "bucket", "role type", "pitch variant", "band"):
        for value, c in sorted(dims.get(dim, {}).items()):
            rate = f"{100 * c['positive'] / c['contacted']:.0f}%" if c["contacted"] else "n/a"
            out.append(f"| {dim} | {value} | {c['contacted']} | {c['positive']} | {rate} | {c['interviews']} |")
    flags = [v for v, c in dims.get("bucket", {}).items() if v != "eligible"
             and c["contacted"] >= cfg["stats"]["stretch_flag_sent"] and c["positive"] == 0]
    out += [""] + [f"> **{f}**: {dims['bucket'][f]['contacted']} sent with no positive reply. Keep pitching it? "
                   "(Not turned off automatically.)" for f in flags]
    band = dims.get("band", {})
    strong, possible = band.get("80+"), band.get("60-79")
    if strong and possible and strong["contacted"] >= 10 and possible["contacted"] >= 10:
        s = strong["positive"] / strong["contacted"]
        p = possible["positive"] / possible["contacted"]
        if s <= p:
            out.append("> Bands look miscalibrated: 80+ jobs reply no better than 60-79. Re-label 20 jobs.")
    return out + [""]


def is_stats_day(now: datetime, cfg: dict) -> bool:
    from pipeline.schedule import PKT
    return now.astimezone(PKT).weekday() == cfg["stats"]["weekday"]
