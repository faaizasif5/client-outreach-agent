"""RemoteOK public API. Their terms ask for a link back and attribution: the report links each
job to its RemoteOK page and names the source."""
import html
from datetime import datetime, timezone

from fetchers.detail_fetch import Http, html_to_text
from fetchers.models import RawJob

API_URL = "https://remoteok.com/api"


def fix_text(value: str) -> str:
    """Unescape HTML and repair double-encoded UTF-8 ("SothebyÃ¢ÂÂs" -> "Sotheby's")."""
    s = html.unescape(value or "").strip()
    for _ in range(2):
        try:
            repaired = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == s:
            break
        s = repaired
    return s


def parse(data: list) -> list[RawJob]:
    jobs = []
    for rank, j in enumerate(x for x in data if isinstance(x, dict) and "id" in x and "position" in x):
        text, links = html_to_text(j.get("description") or "")
        text = fix_text(text)
        posted = None
        if j.get("epoch"):
            posted = datetime.fromtimestamp(int(j["epoch"]), tz=timezone.utc)
        elif j.get("date"):
            posted = datetime.fromisoformat(j["date"])
        location = html.unescape(j.get("location") or "").strip()
        # Every RemoteOK job is remote; the location field says where candidates may be.
        jobs.append(RawJob(
            source="remoteok", external_id=str(j["id"]), url=j.get("url") or "",
            company=fix_text(j.get("company")),
            title=fix_text(j.get("position")),
            location_text=f"Remote ({location})" if location else "Remote",
            description=text, posted_at=posted, apply_url=j.get("apply_url") or j.get("url"),
            links=links, tags=[str(t) for t in j.get("tags") or []], feed_rank=rank))
    return jobs


def fetch(http: Http, now_local: datetime) -> list[RawJob]:
    return parse(http.get(API_URL, as_json=True, ttl_hours=6))
