"""We Work Remotely category RSS feeds."""
import calendar
from datetime import datetime, timezone

import feedparser

from fetchers.detail_fetch import Http, html_to_text
from fetchers.models import RawJob


def parse(feed_text: str) -> list[RawJob]:
    jobs = []
    for rank, e in enumerate(feedparser.parse(feed_text).entries):
        company, _, title = (e.get("title") or "").partition(": ")
        if not title:
            company, title = "", company
        text, links = html_to_text(e.get("summary") or "")
        posted = None
        if e.get("published_parsed"):
            posted = datetime.fromtimestamp(calendar.timegm(e.published_parsed), tz=timezone.utc)
        region = (e.get("region") or "").strip()
        link = e.get("link") or e.get("id") or ""
        jobs.append(RawJob(
            source="wwr", external_id=link.rstrip("/").rsplit("/", 1)[-1], url=link,
            company=company.strip(), title=title.strip(),
            location_text=f"Remote ({region})" if region else "Remote",
            description=text, posted_at=posted, apply_url=link, links=links, feed_rank=rank,
            tags=[t.get("term", "") for t in e.get("tags", [])]))
    return jobs


def fetch(http: Http, now_local: datetime, feeds: list[str]) -> list[RawJob]:
    jobs: list[RawJob] = []
    for url in feeds:
        jobs += parse(http.get(url, ttl_hours=6))
    return jobs
