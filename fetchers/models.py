from datetime import datetime

from pydantic import BaseModel, Field


class RawJob(BaseModel):
    """One listing as a fetcher saw it. Text fields are plain text (HTML already stripped)."""
    source: str                          # hn | remoteok | wwr
    external_id: str
    url: str                             # the listing on the source site
    company: str
    title: str
    location_text: str | None = None
    description: str = ""
    posted_at: datetime | None = None    # the source's own posted date, UTC; never first_seen
    apply_url: str | None = None
    company_url: str | None = None
    links: list[str] = Field(default_factory=list)   # every href found in the listing
    tags: list[str] = Field(default_factory=list)
    feed_rank: int | None = None         # position in a newest-first feed (0 = top)
    replies_text: str = ""               # HN: same-author replies (may say "filled")
