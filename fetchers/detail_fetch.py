"""Polite plain-Python HTTP: per-host delays, on-disk cache, and a hard block on linkedin.com.
Also HTML-to-text and ATS posted-date lookups (freshness fallback #2)."""
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from pipeline.normalize import ats_board

log = logging.getLogger(__name__)


class BlockedHost(Exception):
    pass


class Http:
    def __init__(self, cfg: dict, cache_dir: Path, session=None, sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = cfg["user_agent"]
        self.sleep, self.clock = sleep, clock
        self.last_request: dict[str, float] = {}
        self.requests_made = 0

    def _check_host(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        for blocked in self.cfg.get("blocked_hosts", []):
            if host == blocked or host.endswith("." + blocked):
                raise BlockedHost(f"refusing to fetch {host}")
        return host

    def _wait(self, host: str) -> None:
        last = self.last_request.get(host)
        if last is not None:
            gap = random.uniform(self.cfg["min_delay_seconds"], self.cfg["max_delay_seconds"])
            remaining = gap - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request[host] = self.clock()

    def get(self, url: str, *, as_json: bool = False, ttl_hours: float | None = None):
        host = self._check_host(url)
        ttl = self.cfg["cache_ttl_hours"] if ttl_hours is None else ttl_hours
        path = self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")
        if path.exists() and ttl > 0:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached["fetched_at"] < ttl * 3600:
                return json.loads(cached["body"]) if as_json else cached["body"]
        self._wait(host)
        resp = self.session.get(url, timeout=self.cfg["timeout_seconds"])
        self.requests_made += 1
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        body = resp.text
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "fetched_at": time.time(), "body": body}),
                        encoding="utf-8")
        return json.loads(body) if as_json else body

    def ats_posted_date(self, url: str | None) -> datetime | None:
        """Posting date from Greenhouse / Lever / Ashby public APIs, when the URL is on one."""
        board = ats_board(url)
        if not board:
            return None
        provider, slug, job_id = board
        try:
            if provider == "greenhouse":
                data = self.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
                                as_json=True, ttl_hours=24)
                value = data.get("first_published") or data.get("updated_at")
                return _parse_dt(value)
            if provider == "lever":
                data = self.get(f"https://api.lever.co/v0/postings/{slug}/{job_id}", as_json=True,
                                ttl_hours=24)
                return datetime.fromtimestamp(data["createdAt"] / 1000, tz=timezone.utc)
            if provider == "ashby":
                data = self.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                                as_json=True, ttl_hours=24)
                for job in data.get("jobs", []):
                    if job.get("id") == job_id:
                        return _parse_dt(job.get("publishedAt"))
        except (requests.RequestException, ValueError, KeyError) as e:
            log.info("ATS date lookup failed for %s: %s", url, e)
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def html_to_text(html: str) -> tuple[str, list[str]]:
    """Plain text (paragraphs on new lines) and every href in the fragment."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4"]):
        p.insert_before("\n")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    text = soup.get_text()
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line), links
