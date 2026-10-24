"""Layered job dedupe (spec section 7). Rows are inserted as they arrive, so duplicates within
one run and across runs are handled the same way."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from db import repo
from pipeline.normalize import title_similarity

REPOST_SIMILARITY = 0.85
FILLABLE = ("company_domain", "contact_email", "apply_url", "location_text", "description",
            "team_size", "ats_key")


@dataclass
class Candidate:
    source: str
    external_id: str
    source_url: str
    company: str
    company_norm: str
    company_domain: str | None
    title: str
    title_norm: str
    canonical_url: str | None
    ats_key: str | None
    apply_url: str | None = None
    posted_at: datetime | None = None
    fields: dict = field(default_factory=dict)  # other job columns (location_text, description, ...)

    @property
    def identity(self) -> str:
        return self.company_domain or self.company_norm


def job_key_for(c: Candidate) -> str:
    if c.ats_key:
        return c.ats_key
    if c.external_id:
        return f"src:{c.source}:{c.external_id}"
    return "sha1:" + hashlib.sha1(f"{c.identity}|{c.title_norm}".encode()).hexdigest()


def is_specific_job_url(url: str | None) -> bool:
    """A URL for one posting, not a generic careers page shared by several roles."""
    if not url:
        return False
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    has_id = any(k.lower() in {"id", "jid", "gh_jid", "job", "job_id", "jobid"}
                 for k, _ in parse_qsl(parts.query))
    return len(segments) >= 2 or has_id


def _sources(row) -> list[dict]:
    return json.loads(row["sources"] or "[]")


def _ids_conflict(row, c: Candidate) -> bool:
    """Both carry IDs in the same namespace and the IDs differ -> separate vacancies."""
    if row["ats_key"] and c.ats_key and row["ats_key"] != c.ats_key:
        return True
    return any(s["source"] == c.source and s["external_id"] and c.external_id
               and s["external_id"] != c.external_id for s in _sources(row))


def _same_company_rows(conn, c: Candidate):
    return conn.execute(
        "SELECT * FROM jobs WHERE company_norm = :norm OR "
        "(:domain IS NOT NULL AND company_domain = :domain) ORDER BY first_seen_at",
        {"norm": c.company_norm, "domain": c.company_domain}).fetchall()


def find_match(conn, c: Candidate) -> tuple[str | None, list[str]]:
    """Return (existing job_key or None, flags for a new row)."""
    # 1. ATS job ID or same source external ID
    if c.ats_key:
        row = conn.execute("SELECT job_key FROM jobs WHERE ats_key=? OR job_key=?",
                           (c.ats_key, c.ats_key)).fetchone()
        if row:
            return row["job_key"], []
    if c.external_id:
        row = conn.execute(
            "SELECT job_key FROM jobs WHERE EXISTS (SELECT 1 FROM json_each(jobs.sources) "
            "WHERE json_extract(value, '$.source') = ? AND json_extract(value, '$.external_id') = ?)",
            (c.source, c.external_id)).fetchone()
        if row:
            return row["job_key"], []
    # 2. Same canonical URL (listing URL, or a job-specific apply URL)
    if c.canonical_url:
        row = conn.execute("SELECT job_key FROM jobs WHERE canonical_url=?", (c.canonical_url,)).fetchone()
        if row:
            return row["job_key"], []
    if is_specific_job_url(c.apply_url):
        row = conn.execute("SELECT job_key FROM jobs WHERE apply_url=?", (c.apply_url,)).fetchone()
        if row:
            return row["job_key"], []
    # 3 + 4. Same company identity: exact title -> same job unless IDs conflict; similar -> flag
    flags = []
    for row in _same_company_rows(conn, c):
        if row["title_norm"] == c.title_norm and not _ids_conflict(row, c):
            return row["job_key"], []
        if title_similarity(row["title_norm"], c.title_norm) >= REPOST_SIMILARITY:
            flags.append(f"possible_repost:{row['job_key']}")
    return None, flags[:1]


def upsert(conn, c: Candidate, now: datetime) -> tuple[str, bool, list[str]]:
    """Insert a new job or merge into the matching row. Returns (job_key, created, flags)."""
    match, flags = find_match(conn, c)
    source_entry = {"source": c.source, "external_id": c.external_id, "url": c.source_url}
    posted = repo.iso(c.posted_at) if c.posted_at else None
    if match:
        row = repo.get_job(conn, match)
        updates = {"last_seen_at": repo.iso(now)}
        sources = _sources(row)
        if not any(s["source"] == c.source and s["external_id"] == c.external_id for s in sources):
            sources.append(source_entry)
            updates["sources"] = json.dumps(sources)
        candidate_values = {"company_domain": c.company_domain, "apply_url": c.apply_url,
                            "ats_key": c.ats_key, **c.fields}
        for col in FILLABLE:
            if not row[col] and candidate_values.get(col):
                updates[col] = candidate_values[col]
        # Keep the earliest known posted date: a repost must never make an old job look fresh.
        if posted and (not row["posted_at"] or posted < row["posted_at"]):
            updates["posted_at"] = posted
            updates["posted_at_source"] = c.fields.get("posted_at_source", "source")
        repo.update_job(conn, match, updates)
        return match, False, []
    job_key = job_key_for(c)
    canonical = c.canonical_url
    if canonical and conn.execute("SELECT 1 FROM jobs WHERE canonical_url=?", (canonical,)).fetchone():
        canonical = None
    row = {
        "job_key": job_key, "company": c.company, "company_norm": c.company_norm,
        "company_domain": c.company_domain, "title": c.title, "title_norm": c.title_norm,
        "canonical_url": canonical, "ats_key": c.ats_key, "apply_url": c.apply_url,
        "sources": json.dumps([source_entry]), "posted_at": posted,
        "first_seen_at": repo.iso(now), "last_seen_at": repo.iso(now),
        "flags": json.dumps(flags),
        **{k: v for k, v in c.fields.items() if k in repo.JOB_COLUMNS},
    }
    if posted and "posted_at_source" not in c.fields:
        row["posted_at_source"] = "source"
    repo.insert_job(conn, row)
    return job_key, True, flags
