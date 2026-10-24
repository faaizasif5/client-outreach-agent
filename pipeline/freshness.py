"""Freshness check #1 (spec section 8): age from the source's posted date, never first_seen_at.

Fallback order when the source has no date: relative text -> ATS API date -> newest-first
feed evidence (after a baseline run). Otherwise `age_unknown`."""
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
