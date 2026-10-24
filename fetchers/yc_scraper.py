"""YC startup jobs (spec section 7a): Playwright loads each public results page once a day, no AI.

Pages and extraction rules live in config/yc_selectors.yaml (data, not code):
- yc_board: ycombinator.com's remote engineering board (listings carry their posting age).
- waas_jobs: Work at a Startup's public jobs page (no dates there, so the posting date and the
  founders come from the company's public YC page, fetched once per company and cached).
Detail pages are fetched with plain HTTP (cached, 2-3 s apart) only for listings not already in
the database, and stale listings skip their job page entirely. Public pages only: never logs in."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from fetchers.models import RawJob

ROOT = Path(__file__).resolve().parent.parent
SELECTORS = ROOT / "config" / "yc_selectors.yaml"
BASE = "https://www.ycombinator.com"
AGE = re.compile(r"(?:(about|over|almost)\s+)?(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?", re.I)
UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def load_selectors(path: Path = SELECTORS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def page_rules(doc: dict, name: str) -> dict:
    """One page's rules plus the shared allowed_domains, as used by extract() and the health checks."""
    page = next(p for p in doc["pages"] if p["name"] == name)
    return {**page, "allowed_domains": doc["allowed_domains"]}


def _dig(data, path: str):
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def parse_age(text: str | None, now: datetime) -> datetime | None:
    """'24 days', 'about 2 months', 'over 1 year' -> approximate posted date."""
    m = AGE.search(text or "")
    if not m:
        return None
    qualifier, n, unit = m.group(1), m.group(2).lower(), m.group(3).lower()
    count = 1 if n in ("a", "an", "one") else int(n)
    days = count * UNIT_DAYS[unit]
    if qualifier and qualifier.lower() == "over":
        days += 1          # "over 1 year" is older than a year
    if UNIT_DAYS[unit] == 0:
        return now - timedelta(hours=count if unit == "hour" else count / 60)
    return now - timedelta(days=days)


def _data_page(html: str, container: str, attribute: str):
    node = BeautifulSoup(html or "", "html.parser").select_one(container)
    if node is None or not node.get(attribute):
        return None
    return json.loads(node[attribute])


def extract(html: str, sel: dict) -> list[dict]:
    """Listings as plain dicts (keys from the page's field map). Raises ValueError if the
    container is missing, so health checks can call the page broken."""
    if sel["mode"] == "xhr":
        return _extract_xhr(html, sel)
    if sel["mode"] == "json":
        cfg = sel["json"]
        data = _data_page(html, cfg["container"], cfg["attribute"])
        if data is None:
            raise ValueError("results container not found")
        postings = _dig(data, cfg["listings_path"])
        if not isinstance(postings, list):
            raise ValueError("listings path not found")
        items = [{field: p.get(key) for field, key in cfg["fields"].items()} for p in postings if isinstance(p, dict)]
    else:
        cfg = sel["css"]
        container = BeautifulSoup(html or "", "html.parser").select_one(cfg["container"])
        if container is None:
            raise ValueError("results container not found")
        items = []
        for card in container.select(cfg["card"]):
            item = {}
            for field, rule in cfg["fields"].items():
                el = card.select_one(rule["selector"])
                item[field] = None if el is None else (el.get_text(" ", strip=True) if rule["attr"] == "text"
                                                       else el.get(rule["attr"]))
            items.append(item)
    if sel.get("url_template"):
        for item in items:
            if not item.get("url") and item.get("id"):
                item["url"] = sel["url_template"].format(**{k: v for k, v in item.items() if v is not None})
    return items


def _extract_xhr(payload: str, sel: dict) -> list[dict]:
    """Logged-in companies page: captured background responses, flattened to one item per job.
    Only engineering jobs that are remote (yes/only) are kept (`include` in the YAML)."""
    cfg = sel["xhr"]
    try:
        data = json.loads(payload or "")
    except json.JSONDecodeError:
        raise ValueError("results container not found")
    companies = _dig(data, cfg["companies_path"]) if isinstance(data, dict) else None
    if not isinstance(companies, list):
        raise ValueError("listings path not found")
    include = cfg.get("include") or {}
    items = []
    for company in companies:
        base = {field: company.get(key) for field, key in cfg["company_fields"].items()}
        for job in company.get(cfg["jobs_key"]) or []:
            item = {**base, **{field: job.get(key) for field, key in cfg["job_fields"].items()}}
            if all(str(item.get(field)) in [str(v) for v in allowed] for field, allowed in include.items()):
                items.append(item)
    return items


def absolute(url: str | None) -> str | None:
    return urljoin(BASE, url) if url else None


def on_allowed_domain(url: str | None, sel: dict) -> bool:
    host = urlsplit(absolute(url) or "").hostname or ""
    return host in sel["allowed_domains"]


def parse_detail(html: str, sel: dict) -> dict:
    """yc_job detail: the ycombinator.com job page carries job + company + founders."""
    cfg = sel["detail"]
    data = _data_page(html, cfg["container"], cfg["attribute"])
    if data is None:
        return {}
    return {"job": _dig(data, cfg["job_path"]) or {}, "company": _dig(data, cfg["company_path"]) or {}}


def parse_company_page(html: str, sel: dict, job_id) -> dict:
    """waas detail, step 1: the public YC company page -> company, founders and this job's posting
    data (createdAt, visa, experience)."""
    cfg = sel["detail"]
    data = _data_page(html, cfg["container"], cfg["attribute"])
    if data is None:
        return {}
    postings = _dig(data, cfg["postings_path"]) or []
    job = next((p for p in postings if str(p.get("id")) == str(job_id)), {})
    return {"company": _dig(data, cfg["company_path"]) or {}, "job": dict(job)}


def parse_waas_job(html: str, sel: dict) -> str:
    """waas detail, step 2: job description (plain text) from the WaaS job page."""
    from fetchers.detail_fetch import html_to_text
    cfg = sel["detail"]
    data = _data_page(html, cfg["container"], cfg["attribute"])
    job = _dig(data, cfg["job_path"]) if data else None
    return html_to_text((job or {}).get(cfg["description_key"]) or "")[0]


def to_raw(listing: dict, detail: dict, now: datetime, rank: int) -> RawJob:
    job, company = detail.get("job") or {}, detail.get("company") or {}
    company = {**{k: listing.get(k) for k in ("team_size", "one_liner", "website") if listing.get(k)}, **company}
    facts = [f"Location: {listing.get('location') or job.get('location') or ''}",
             f"Visa: {listing.get('visa') or job.get('visa') or job.get('sponsorsVisa') or ''}",
             f"Experience: {listing.get('experience') or job.get('minExperience') or ''}",
             f"Salary: {listing.get('salary') or job.get('salaryRange') or ''}"]
    team = company.get("team_size") or company.get("teamSize")
    if team:
        facts.append(f"Team size: {team} employees")
    if company.get("one_liner"):
        facts.append(f"Company: {company['one_liner']}")
    description = "\n".join(facts) + "\n\n" + (job.get("description") or listing.get("description") or "")
    url = absolute(listing.get("url"))
    return RawJob(
        source="yc", external_id=str(listing.get("id")), url=url or "",
        company=listing.get("company") or company.get("name") or "", title=listing.get("title") or "",
        location_text=listing.get("location") or job.get("location"),
        description=description,
        posted_at=detail.get("posted_at") or parse_age(listing.get("created") or job.get("createdAt"), now),
        apply_url=url, company_url=company.get("website") or company.get("url") or None,
        links=[l for l in (company.get("website") or company.get("url"),) if l], feed_rank=rank,
        tags=[listing.get("role") or ""])


def founders(detail: dict) -> list[dict]:
    """Founders from the company's public YC data: verified (spec: YC page -> Verified yes)."""
    out = []
    for f in (detail.get("company") or {}).get("founders") or []:
        if f.get("full_name") and f.get("is_active", True):
            out.append({"name": f["full_name"], "title": f.get("title") or "Founder",
                        "linkedin_url": (f.get("linkedin_url") or f.get("linkedin") or "").rstrip("/") or None,
                        "source": "yc_page", "verified": 1})
    return out


def load_results_page(url: str, user_agent: str, timeout_ms: int = 60000) -> tuple[int, str]:
    """Headless Chromium, public page, no login. A redirect away from the requested path (e.g.
    to a login or home page) counts as a failed load."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=user_agent)
            resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if urlsplit(page.url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                return 302, page.content()
            return (resp.status if resp else 0), page.content()
        finally:
            browser.close()


def parse_us_date(text: str | None) -> datetime | None:
    """'10/21/2026' (Work at a Startup's updated date) -> datetime (UTC midnight)."""
    from datetime import timezone
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", text or "")
    return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc) if m else None


def _detail(page: dict, listing: dict, http, now: datetime, max_age_days: int) -> dict:
    if page["detail"]["kind"] == "inline":
        # A job last updated over max_age_days ago was also created then: stale, no request needed.
        updated = parse_us_date(listing.get("updated"))
        company = {"name": listing.get("company"), "website": listing.get("website"),
                   "team_size": listing.get("team_size"), "one_liner": listing.get("one_liner"),
                   "founders": listing.get("founders") or []}
        if updated and now - updated > timedelta(days=max_age_days):
            return {"job": {}, "company": company, "posted_at": updated}
        url = page["detail"]["company_url_template"].format(company_slug=listing.get("company_slug"))
        public = parse_company_page(http.get(url, ttl_hours=24), page, listing["id"])
        return {"job": public.get("job") or {}, "company": company}
    if page["detail"]["kind"] == "yc_job":
        return parse_detail(http.get(absolute(listing["url"]), ttl_hours=24 * 30), page)
    # waas: dates + founders from the YC company page (cached a week), text from the job page
    company_url = page["detail"]["company_url_template"].format(company_slug=listing.get("company_slug"))
    detail = parse_company_page(http.get(company_url, ttl_hours=24 * 7), page, listing["id"])
    posted = parse_age(detail.get("job", {}).get("createdAt"), now)
    if posted and now - posted <= timedelta(days=max_age_days):   # stale jobs skip their job page
        detail["job"]["description"] = parse_waas_job(http.get(listing["url"], ttl_hours=24 * 30), page)
    return detail


def _broken_page(conn, page, result, html, now, info, selectors_path, snapshots, cfg, claude) -> None:
    import os
    from db import repo
    from fetchers import yc_health, yc_repair
    from pipeline.claude_cli import run_claude
    name = page["name"]
    if page.get("repair") is False:   # logged-in page: no snapshot, never sent to Claude
        yc_health.record(conn, result, 0, now, name)
        info["manual_fix_needed"] = True
        if result["checks"].get("page_error") == "HTTP 401":
            info["notes"].append(f"YC page {name}: your session expired. Run: python main.py yc-login")
        else:
            info["notes"].append(f"YC page {name} broken ({result['checks']}); fix config/yc_selectors.yaml by hand.")
        return
    snap = yc_health.save_snapshot(html, snapshots, now, name)
    yc_health.record(conn, result, 0, now, name)
    info["notes"].append(f"YC page {name} broken ({result['checks']}); snapshot {snap.name}; skipped this run.")
    repair = yc_repair.attempt(conn, snap.read_text(encoding="utf-8"), selectors_path, now, page=name,
                               model=cfg.get("yc", {}).get("repair_model", "sonnet"), claude=claude or run_claude)
    info["repair"][name] = {k: v for k, v in repair.items() if k != "usage"}
    if repair.get("usage"):
        info["repair_usage"] = repair["usage"]
    if repair["accepted"]:
        info["notes"].append(f"YC page {name}: selectors repaired automatically (backup {repair.get('backup')}).")
        return
    info["manual_fix_needed"] = True
    info["notes"].append(f"YC page {name}: automatic repair {repair['reason']}. Manual fix: prompts/yc_manual_fix.md.")
    last_ok = conn.execute("SELECT MAX(checked_at) FROM yc_health WHERE status='healthy' AND page=?",
                           (name,)).fetchone()[0]
    first_broken = conn.execute("SELECT MIN(checked_at) FROM yc_health WHERE status='broken' AND page=? "
                                "AND checked_at > ?", (name, last_ok or "")).fetchone()[0]
    if first_broken and repo.parse_iso(first_broken) <= now - timedelta(days=14):
        doc = load_selectors(selectors_path)
        for p in doc["pages"]:
            if p["name"] == name:
                p["enabled"] = False
        selectors_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        conn.execute("INSERT INTO yc_repairs (attempted_at, kind, accepted, details, page) VALUES (?,?,0,?,?)",
                     (repo.iso(now), "disabled", "broken for 14 days", name))
        info["notes"].append(f"YC page {name} broken for 14 days: disabled in config/yc_selectors.yaml.")
        if not any(p.get("enabled", True) for p in doc["pages"] if p.get("repair", True) is not False):
            env = ROOT / ".env"
            lines = [l for l in (env.read_text(encoding="utf-8").splitlines() if env.exists() else [])
                     if not l.startswith("YC_ENABLED=")]
            env.write_text("\n".join(lines + ["YC_ENABLED=false"]) + "\n", encoding="utf-8")
            os.environ["YC_ENABLED"] = "false"
            info["notes"].append("All YC pages broken for 14 days: YC disabled (YC_ENABLED=false). Other sources unaffected.")


def fetch(conn, http, now: datetime, *, cfg: dict, load_page=None, claude=None,
          selectors_path: Path = SELECTORS, snapshots: Path = ROOT / "snapshots" / "yc") -> tuple[list[RawJob], dict]:
    """One run over every enabled page: results page -> health checks -> (healthy) new listings
    + founders, or (broken) snapshot + repair attempt. Returns (raw jobs, info for the digest)."""
    import os
    from db import repo
    from fetchers import yc_health
    from pipeline import people as people_mod
    from pipeline.normalize import norm_company

    info = {"status": None, "new": 0, "notes": [], "pages": {}, "repair": {}}
    if os.environ.get("YC_ENABLED", "true").lower() == "false":
        info.update(status="disabled", notes=["YC scraper is disabled (YC_ENABLED=false in .env)."])
        return [], info
    doc = load_selectors(selectors_path)
    raws, seen_ids = [], set()
    max_age = cfg.get("max_age_days", 14)
    for page_cfg in [p for p in doc["pages"] if p.get("enabled", True)]:
        page = page_rules(doc, page_cfg["name"])
        name = page["name"]
        if page.get("requires_session") and not load_page and not has_session():
            info["notes"].append(f"YC page {name} skipped: no saved session (python main.py yc-login).")
            continue
        if page["mode"] == "xhr" and not load_page:
            status, html = load_logged_in_companies(page["results_url"], page["xhr"])
        else:
            status, html = (load_page or load_results_page)(page["results_url"], cfg["http"]["user_agent"])
        result = yc_health.check(status, html, page, yc_health.recent_counts(conn, page=name))
        info["pages"][name] = {"status": result["status"], "count": result["count"]}
        if result["status"] == "broken":
            _broken_page(conn, page, result, html, now, info, selectors_path, snapshots, cfg, claude)
            continue
        new = 0
        for rank, listing in enumerate(result["listings"]):
            lid = str(listing.get("id") or "")
            if not lid or lid in seen_ids or not on_allowed_domain(listing.get("url"), page):
                continue   # same YC job id on both pages -> read once
            seen_ids.add(lid)
            known = conn.execute(
                "SELECT description FROM jobs WHERE EXISTS (SELECT 1 FROM json_each(jobs.sources) WHERE "
                "json_extract(value,'$.source')='yc' AND json_extract(value,'$.external_id')=?)", (lid,)).fetchone()
            if known:   # detail pages only for listings not already in the database
                raw = to_raw(listing, {}, now, rank)
                raw.description = known["description"] or raw.description
                raws.append(raw)
                continue
            detail = {}
            try:
                detail = _detail(page, listing, http, now, max_age) if http else {}
            except Exception as e:
                info["notes"].append(f"YC detail fetch failed for {listing.get('company')}: {type(e).__name__}")
            raw = to_raw(listing, detail, now, rank)
            raws.append(raw)
            new += 1
            company_norm = norm_company(raw.company)
            if detail.get("company") and not people_mod.page_fetched(conn, company_norm, "yc_page"):
                found = founders(detail)
                people_mod.save_people(conn, company_norm, found[:people_mod.MAX_PEOPLE], now)
                conn.execute("INSERT OR REPLACE INTO company_pages (company_norm, source, url, fetched_at, "
                             "people_found) VALUES (?,?,?,?,?)",
                             (company_norm, "yc_page", raw.url, repo.iso(now), len(found)))
        info["pages"][name]["new"] = new
        info["new"] += new
        if yc_health.freshness_warning(conn, new, page=name):
            info["notes"].append(f"YC page {name}: no new listings for 7 runs in a row (warning only).")
        yc_health.record(conn, result, new, now, name)
    statuses = {p["status"] for p in info["pages"].values()}
    info["status"] = ("healthy" if statuses == {"healthy"} else "broken" if statuses == {"broken"}
                      else "partial" if statuses else "no pages enabled")
    return raws, info


# ---------------------------------------------------------------- logged-in pages (user's own session)

PROFILE_DIR = ROOT / "data" / "yc_browser_profile"   # your session cookies; gitignored (data/)
LOGIN_URL = "https://www.workatastartup.com/companies"


def interactive_login(profile_dir: Path = PROFILE_DIR) -> None:
    """Open a visible browser with a persistent profile. YOU log in; the tool never sees or stores
    your password. Close the window when the companies page is showing."""
    from playwright.sync_api import sync_playwright
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=False, no_viewport=True,
                                                   args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print("A browser window is open. Log in to Work at a Startup there, open the companies page, "
              "then close the window.", flush=True)
        try:   # a slow page must not close the window: you can still log in by hand
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=120000)
            page.bring_to_front()
        except Exception as e:
            print(f"(page still loading: {type(e).__name__}; keep going in the window)", flush=True)
        while ctx.pages:   # wait until you close every tab/window
            try:
                ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                break
        ctx.close()


def has_session(profile_dir: Path = PROFILE_DIR) -> bool:
    return profile_dir.is_dir() and any(profile_dir.iterdir())


def load_logged_in_page(url: str, user_agent: str | None = None, timeout_ms: int = 60000,
                        profile_dir: Path = PROFILE_DIR) -> tuple[int, str]:
    """Headless load with your saved session. Redirected away (login/home) -> 401: session expired."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            page = ctx.new_page()
            resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if urlsplit(page.url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                return 401, page.content()
            return (resp.status if resp else 0), page.content()
        finally:
            ctx.close()



def load_logged_in_companies(url: str, xhr: dict, profile_dir: Path = PROFILE_DIR,
                             timeout_ms: int = 90000) -> tuple[int, str]:
    """Headless load with your saved session; keeps only the matching background responses
    (company + job data), scrolling for up to max_batches. Redirect away -> 401 (session expired).
    Nothing else from the page (your profile, messages) is kept."""
    from playwright.sync_api import sync_playwright
    batches = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=True)
        try:
            page = ctx.new_page()

            def keep(resp):
                if xhr["match"] in resp.url and resp.request.method == "POST":
                    try:
                        batches.append(resp.json())
                    except Exception:
                        pass
            page.on("response", keep)
            resp = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if urlsplit(page.url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                return 401, ""
            seen = len(batches)
            for _ in range(xhr["max_batches"] - 1):
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(2500)      # polite: one batch at a time
                if len(batches) == seen:
                    break
                seen = len(batches)
            status = resp.status if resp else 0
        finally:
            ctx.close()
    return status, json.dumps({xhr["companies_path"]: prune_companies(batches[:xhr["max_batches"]], xhr)})


def prune_companies(batches: list[dict], xhr: dict) -> list[dict]:
    """Keep only the company/job fields the YAML names (drops anything tied to your account,
    such as saved-search state attached to each job)."""
    company_keys = set(xhr["company_fields"].values()) | {"id"}
    job_keys = set(xhr["job_fields"].values())
    out, ids = [], set()
    for b in batches:
        for c in b.get(xhr["companies_path"]) or []:
            if c.get("id") in ids:
                continue
            ids.add(c.get("id"))
            company = {k: v for k, v in c.items() if k in company_keys}
            company["founders"] = [{k: f.get(k) for k in ("full_name", "linkedin", "title") if k in f}
                                   for f in c.get("founders") or []]
            company[xhr["jobs_key"]] = [{k: v for k, v in j.items() if k in job_keys}
                                        for j in c.get(xhr["jobs_key"]) or []]
            out.append(company)
    return out
