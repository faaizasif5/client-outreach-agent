"""Health checks for the YC scraper, every run (spec 7a)."""
import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from db import repo
from fetchers import yc_scraper

MIN_RATIO = 0.30
MIN_FIELD_QUALITY = 0.90


def check(status: int, html: str, sel: dict, recent_counts: list[int]) -> dict:
    """{'status': healthy|broken, 'checks': {...}, 'listings': [...]}"""
    checks = {}
    try:
        listings = yc_scraper.extract(html, sel) if status == 200 else []
        checks["page_loads"] = status == 200
        if status != 200:
            checks["page_error"] = f"HTTP {status}"
    except (ValueError, json.JSONDecodeError) as e:
        listings, checks["page_loads"], checks["page_error"] = [], False, str(e)
    n = len(listings)
    avg = sum(recent_counts) / len(recent_counts) if recent_counts else None
    checks["listing_count"] = n > 0 and (avg is None or n >= MIN_RATIO * avg)
    complete = [l for l in listings if l.get("title") and l.get("company") and l.get("url")]
    checks["field_quality"] = bool(listings) and len(complete) / len(listings) >= MIN_FIELD_QUALITY
    checks["url_safety"] = bool(listings) and all(yc_scraper.on_allowed_domain(l.get("url"), sel) for l in listings)
    healthy = all(checks[k] for k in ("page_loads", "listing_count", "field_quality", "url_safety"))
    return {"status": "healthy" if healthy else "broken", "checks": checks, "listings": listings,
            "count": n, "average": avg}


def recent_counts(conn, limit: int = 7, page: str | None = None) -> list[int]:
    return [r[0] for r in conn.execute("SELECT listing_count FROM yc_health WHERE status='healthy' "
                                       "AND (? IS NULL OR page = ?) ORDER BY checked_at DESC LIMIT ?",
                                       (page, page, limit))]


def freshness_warning(conn, new_count: int, page: str | None = None) -> bool:
    """True if no new listings for 7 runs in a row (warning only)."""
    last = [r[0] for r in conn.execute("SELECT new_count FROM yc_health WHERE (? IS NULL OR page = ?) "
                                       "ORDER BY checked_at DESC LIMIT 6", (page, page))]
    return len(last) == 6 and new_count == 0 and not any(last)


def record(conn, result: dict, new_count: int, now: datetime, page: str | None = None) -> None:
    conn.execute("INSERT INTO yc_health (checked_at, status, listing_count, new_count, details, page) "
                 "VALUES (?,?,?,?,?,?)", (repo.iso(now), result["status"], result["count"], new_count,
                                          json.dumps(result["checks"]), page))


def strip_snapshot(html: str, max_listings: int = 12) -> str:
    """Untrusted page -> safe debug snapshot: no scripts, styles or inline handlers, main content
    only; embedded JSON listings trimmed so a repair prompt stays small."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "link", "meta"]):
        tag.decompose()
    for el in soup.find_all(True):
        for attr in [a for a in el.attrs if a.lower().startswith("on")]:
            del el[attr]
        if el.get("data-page"):
            try:
                data = json.loads(el["data-page"])
                for key, value in (data.get("props") or {}).items():
                    if isinstance(value, list):
                        data["props"][key] = value[:max_listings]
                el["data-page"] = json.dumps(data)
            except ValueError:
                pass
    body = soup.body or soup
    text = str(body)
    return re.sub(r"\n\s*\n+", "\n", text)


def save_snapshot(html: str, folder: Path, now: datetime, page: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (f"{now:%Y-%m-%d}-{page}.html" if page else f"{now:%Y-%m-%d}.html")
    path.write_text(strip_snapshot(html), encoding="utf-8")
    return path
