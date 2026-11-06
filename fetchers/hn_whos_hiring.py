"""HN "Who is hiring?" via the Algolia API: current month's thread only (spec section 8)."""
import html
import re
from datetime import datetime, timezone

from fetchers.detail_fetch import Http, html_to_text
from fetchers.models import RawJob
from pipeline.contacts import is_apply_url
from pipeline.normalize import is_generic_domain

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
THREAD_TITLE = re.compile(r"who is hiring\?\s*\((\w+)\s+(\d{4})\)", re.I)
ROLE_WORD = re.compile(r"engineer|developer|swe\b|programmer|full[- ]?stack|back[- ]?end|front[- ]?end|"
                       r"software|roles?\b|positions?\b", re.I)
SEPARATOR = re.compile(r"\s*\|\s*|\s+[–—]\s+")
URL_RE = re.compile(r"https?://\S+")


def current_thread_id(search: dict, now_local: datetime) -> str | None:
    month = now_local.strftime("%B").lower()
    for hit in search.get("hits", []):
        m = THREAD_TITLE.search(hit.get("title") or "")
        if m and m.group(1).lower() == month and int(m.group(2)) == now_local.year:
            return str(hit["objectID"])
    return None


def parse_comment(child: dict) -> RawJob | None:
    if not child.get("text") or child.get("type") != "comment":
        return None
    text, links = html_to_text(child["text"])
    links = [html.unescape(l) for l in links]
    lines = text.splitlines()
    if not lines:
        return None
    header = URL_RE.sub("", lines[0]).strip()
    segments = [s.strip() for s in SEPARATOR.split(header) if s.strip()]
    if not segments:
        return None
    company = re.sub(r"\s*\((?!\s*yc\b)[^)]*\)", "", segments[0], flags=re.I).strip() or segments[0]
    title = next((s for s in segments[1:] if ROLE_WORD.search(s)), None)
    if not title:
        line = next((l for l in lines[1:6] if re.search(r"engineer|developer", l, re.I)), "See post")
        title = SEPARATOR.split(line.lstrip("-•* "))[0][:120]
    company_url = next((l for l in links if l.startswith("http") and not is_generic_domain(l)
                        and not is_apply_url(l)), None)
    replies = " ".join(html_to_text(r.get("text") or "")[0] for r in child.get("children", [])
                       if r.get("author") == child.get("author"))
    return RawJob(
        source="hn", external_id=str(child["id"]),
        url=f"https://news.ycombinator.com/item?id={child['id']}",
        company=company, title=title, location_text=header, description=text,
        posted_at=datetime.fromtimestamp(int(child["created_at_i"]), tz=timezone.utc),
        company_url=company_url, links=links, replies_text=replies)


def parse_thread(item: dict) -> list[RawJob]:
    jobs = []
    for rank, child in enumerate(item.get("children", [])):
        if str(child.get("parent_id")) != str(item.get("id")):
            continue
        job = parse_comment(child)
        if job:
            jobs.append(job)
    return jobs


def fetch(http: Http, now_local: datetime) -> list[RawJob]:
    search = http.get(SEARCH_URL, as_json=True, ttl_hours=1)
    story_id = current_thread_id(search, now_local)
    if not story_id:
        return []
    return parse_thread(http.get(ITEM_URL.format(id=story_id), as_json=True, ttl_hours=3))
