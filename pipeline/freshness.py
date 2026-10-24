"""Freshness check #1 (spec section 8): age from the source's posted date, never first_seen_at.

Fallback order when the source has no date: relative text -> ATS API date -> newest-first
feed evidence (after a baseline run). Otherwise `age_unknown`."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

RELATIVE = [
    (re.compile(r"\b(?:posted\s+)?(\d{1,3})\+?\s*(minute|min|hour|hr|day|week|month)s?\s+ago\b", re.I), None),
    (re.compile(r"\b(\d{1,3})\s*(m|h|d|w|mo)\s+ago\b", re.I), None),
    (re.compile(r"\bposted\s+(today|yesterday)\b", re.I), "word"),
]
UNIT_DAYS = {"minute": 0, "min": 0, "m": 0, "hour": 0, "hr": 0, "h": 0, "day": 1, "d": 1,
             "week": 7, "w": 7, "month": 30, "mo": 30}


@dataclass
class Age:
    status: str                 # fresh | stale | age_unknown
    posted_at: datetime | None
    source: str | None          # source | relative_text | ats | newest_first
    estimated: bool = False


def parse_relative(text: str, now: datetime) -> datetime | None:
    for pattern, kind in RELATIVE:
        m = pattern.search(text or "")
        if not m:
            continue
        if kind == "word":
            return now - timedelta(days=0 if m.group(1).lower() == "today" else 1)
        n, unit = int(m.group(1)), m.group(2).lower()
        if UNIT_DAYS[unit] == 0:
            hours = n / 60 if unit in ("minute", "min", "m") else n
            return now - timedelta(hours=hours)
        return now - timedelta(days=n * UNIT_DAYS[unit])
    return None


def _judge(posted: datetime, now: datetime, max_age_days: int, source: str, estimated=False) -> Age:
    posted = min(posted, now)
    status = "fresh" if now - posted <= timedelta(days=max_age_days) else "stale"
    return Age(status, posted, source, estimated)


def assess(posted_at: datetime | None, text: str, now: datetime, max_age_days: int,
           ats_date: Callable[[], datetime | None] | None = None,
           newest_first_since: datetime | None = None) -> Age:
    if posted_at:
        return _judge(posted_at, now, max_age_days, "source")
    rel = parse_relative(text, now)
    if rel:
        return _judge(rel, now, max_age_days, "relative_text")
    if ats_date:
        found = ats_date()
        if found:
            return _judge(found, now, max_age_days, "ats")
    if newest_first_since:
        # Not in the feed on the previous run but now above listings seen then:
        # posted after that run, so its age is at most now - previous run.
        return _judge(newest_first_since, now, max_age_days, "newest_first", estimated=True)
    return Age("age_unknown", None, None)


def newest_first_evidence(seen_before: list[bool]) -> set[int]:
    """Indices (feed order, newest first) of unseen items that sit above a previously seen one."""
    evidence = set()
    for i, seen in enumerate(seen_before):
        if not seen and any(seen_before[i + 1:]):
            evidence.add(i)
    return evidence


def is_closed_text(text: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, text or "", re.I | re.M):
            return True
    return False


# ---------------------------------------------------------------- check #2: liveness (before drafting)

CLOSED_PAGE = re.compile(
    r"no longer (accepting|available|open)|positions? (has been |is )?filled|"
    r"job (is )?(no longer|not) available|this (job|position|posting|role) (has )?(expired|closed|been closed)|"
    r"job not found|posting (is )?closed|position (is )?closed", re.I)
HOME_PATHS = {"", "/", "/careers", "/jobs", "/join", "/careers/", "/jobs/", "/about/careers", "/company/careers"}
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"


def _hn_status(item: dict, closed_patterns) -> str | None:
    if not item.get("text"):
        return "hn_deleted"
    from fetchers.detail_fetch import html_to_text
    replies = " ".join(html_to_text(c.get("text") or "")[0] for c in item.get("children", [])
                       if c.get("author") == item.get("author"))
    if is_closed_text(f"{html_to_text(item['text'])[0]}\n{replies}", closed_patterns):
        return "hn_filled"
    return None


def _ats_missing(http, apply_url: str | None) -> bool | None:
    """True if the posting is gone from its public ATS board, None if not on an ATS."""
    from pipeline.normalize import ats_board
    board = ats_board(apply_url)
    if not board:
        return None
    provider, slug, job_id = board
    if provider == "greenhouse":
        status, _, _ = http.fetch_status(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
        return status == 404
    if provider == "lever":
        status, _, _ = http.fetch_status(f"https://api.lever.co/v0/postings/{slug}/{job_id}")
        return status == 404
    status, _, text = http.fetch_status(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if status == 404:
        return True
    return job_id not in {j.get("id") for j in json.loads(text or "{}").get("jobs", [])}


def check_liveness(job, http, closed_patterns: list[str], *, now: datetime, fetched_sources: set[str],
                   complete_feeds: set[str]) -> tuple[str, str]:
    """('open' | 'closed' | 'unknown', why). Unknown jobs are not drafted this run."""
    from urllib.parse import urlsplit
    sources = json.loads(job["sources"] or "[]")
    names = {s["source"] for s in sources}
    try:
        # Gone from a feed that lists every active job, after being seen there before.
        if names and names <= complete_feeds and names <= fetched_sources:
            last_seen = datetime.fromisoformat(job["last_seen_at"].replace("Z", "+00:00"))
            if last_seen < now.replace(microsecond=0):   # stored timestamps are whole seconds
                return "closed", "gone_from_feed"
        for s in sources:
            if s["source"] == "hn":
                status, _, text = http.fetch_status(HN_ITEM.format(id=s["external_id"]))
                if status == 404:
                    return "closed", "hn_deleted"
                why = _hn_status(json.loads(text or "{}"), closed_patterns)
                if why:
                    return "closed", why
        missing = _ats_missing(http, job["apply_url"])
        if missing:
            return "closed", "ats_missing"
        url = job["apply_url"] or next((s["url"] for s in sources if s["source"] != "hn"), None)
        if not url:
            return "open", "hn_only"
        if "linkedin.com" in (urlsplit(url).hostname or ""):
            return "unknown", "unverifiable_host"
        status, final, text = http.fetch_status(url)
    except (OSError, ValueError) as e:   # requests errors subclass OSError
        return "unknown", f"error: {type(e).__name__}"
    if status in (404, 410):
        return "closed", f"http_{status}"
    if status >= 400:
        return "unknown", f"http_{status}"
    orig_path, final_path = urlsplit(url).path.rstrip("/"), urlsplit(final).path.rstrip("/")
    if final_path != orig_path and final_path in HOME_PATHS and orig_path not in HOME_PATHS:
        return "closed", "redirected_home"
    if CLOSED_PAGE.search(text or ""):
        return "closed", "closed_text"
    return "open", "ok"
