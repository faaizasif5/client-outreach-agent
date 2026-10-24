"""People to connect with on LinkedIn (spec section 11, Network tab).

Sources, zero cost, in order: YC company page (via the YC scraper) -> the company's own
About/Team page -> a name with no profile link (e.g. a signed HN post) -> a LinkedIn people-search
link. linkedin.com is NEVER fetched: we only store links found on other sites, or build search
links that the user opens. Max 2 people per company, founders/CEO/CTO first."""
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from db import repo
from pipeline.normalize import canonical_url

TEAM_PATHS = ("/about", "/team", "/about-us", "/company")
LINKEDIN_PROFILE = re.compile(r"https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/[\w%-]+/?", re.I)
NAME = re.compile(r"^[A-Z][a-zA-Z'’-]+(?: [A-Z][a-zA-Z'’.-]+){1,3}$")
ROLE_PRIORITY = [
    (0, re.compile(r"\b(co-?founder|founder|ceo|chief executive|cto|chief technology)\b", re.I)),
    (1, re.compile(r"\b(head of engineering|vp,? engineering|vp of engineering|engineering (lead|manager)|"
                   r"director of engineering|tech lead)\b", re.I)),
]
SIGNATURE = [
    re.compile(r"\b(?:I'm|I am|my name is|this is)\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,2}),?\s+(?:the\s+|a\s+)?"
               r"((?:co-?founder|founder|ceo|cto|head of engineering|engineering manager|vp engineering)[^.,\n]{0,30})", re.I),
    re.compile(r"^[-–—~]\s*([A-Z][a-z]+(?: [A-Z][a-z]+){1,2})\s*[,(]\s*((?:co-?founder|founder|ceo|cto|"
               r"head of engineering|engineering manager)[^)\n]{0,30})\)?\s*$", re.I | re.M),
]
MAX_PEOPLE = 2


def priority(title: str | None) -> int | None:
    for rank, rx in ROLE_PRIORITY:
        if rx.search(title or ""):
            return rank
    return None


def search_link(name: str, company: str) -> str:
    """Built, never fetched: the user opens it and checks the person."""
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(f'{name} {company}')}"


def from_team_page(html: str, base_url: str) -> list[dict]:
    """Names + titles next to linkedin.com/in links on a company's own page."""
    soup = BeautifulSoup(html or "", "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if not LINKEDIN_PROFILE.match(href):
            continue
        block, name, title = a, None, None
        for _ in range(4):   # walk up to the person's card
            block = block.parent
            if block is None:
                break
            profiles = {x["href"] for x in block.find_all("a", href=True) if LINKEDIN_PROFILE.match(urljoin(base_url, x["href"]))}
            if len(profiles) > 1:   # left this person's card
                break
            lines = [" ".join(t.split()) for t in block.get_text("\n").splitlines() if t.strip()]
            name = name or next((l for l in lines if NAME.match(l) and priority(l) is None), None)
            title = title or next((l for l in lines if priority(l) is not None and len(l) < 60), None)
            if name and title:
                break
        if name and priority(title) is not None:
            found.setdefault(name, {"name": name, "title": title, "linkedin_url": canonical_url(href),
                                    "source": "team_page", "verified": 1})
    return _top(list(found.values()))


def from_listing(text: str, founder_links: list[str], company: str) -> list[dict]:
    """A signed post ('I'm Jane Doe, CTO') -> search link; a linkedin.com/in link in the listing
    is kept but unverified (it came from the job board, not the company's site)."""
    people = []
    for rx in SIGNATURE:
        for m in rx.finditer(text or ""):
            name, title = m.group(1).strip(), m.group(2).strip()
            people.append({"name": name, "title": title, "linkedin_url": search_link(name, company),
                           "source": "search", "verified": 0})
    for link in founder_links:
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        people.append({"name": f"(profile {slug})", "title": "linked in the listing", "linkedin_url": link,
                       "source": "listing", "verified": 0})
    return _top(people)


def _top(people: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in sorted(people, key=lambda p: (priority(p.get("title")) if priority(p.get("title")) is not None else 9,
                                           -p.get("verified", 0))):
        if p["name"] not in seen:
            seen.add(p["name"])
            out.append(p)
    return out[:MAX_PEOPLE]


def save_people(conn, company_norm: str, people: list[dict], now) -> None:
    for p in people:
        conn.execute("INSERT OR IGNORE INTO people (company_norm, name, title, linkedin_url, source, verified, "
                     "fetched_at) VALUES (?,?,?,?,?,?,?)",
                     (company_norm, p["name"], p.get("title"), p.get("linkedin_url"), p["source"],
                      p.get("verified", 0), repo.iso(now)))


def page_fetched(conn, company_norm: str, source: str) -> bool:
    return conn.execute("SELECT 1 FROM company_pages WHERE company_norm=? AND source=?",
                        (company_norm, source)).fetchone() is not None


def fetch_team_page(conn, http, company_norm: str, domain: str | None, now) -> int:
    """Try the company's About/Team pages once per company, ever."""
    if not domain or page_fetched(conn, company_norm, "team_page"):
        return 0
    people, used = [], None
    for path in TEAM_PATHS:
        url = f"https://{domain}{path}"
        try:
            status, final, html = http.fetch_status(url)
        except Exception:
            continue
        if status == 200:
            people = from_team_page(html, final)
            used = url
            if people:
                break
    save_people(conn, company_norm, people, now)
    conn.execute("INSERT OR REPLACE INTO company_pages (company_norm, source, url, fetched_at, people_found) "
                 "VALUES (?,?,?,?,?)", (company_norm, "team_page", used, repo.iso(now), len(people)))
    return len(people)


def people_for(conn, company_norm: str) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM people WHERE company_norm=?", (company_norm,))]
    return _top(rows)


def verified_profile_links(conn, company_norm: str) -> list[str]:
    """Founder LinkedIn links from the YC page or company site (for routing stretch jobs)."""
    return [r["linkedin_url"] for r in conn.execute(
        "SELECT linkedin_url FROM people WHERE company_norm=? AND verified=1 AND linkedin_url LIKE '%linkedin.com/in/%'",
        (company_norm,))]
