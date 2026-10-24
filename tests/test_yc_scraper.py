"""YC scraper (spec 7a): two public results pages, saved snapshots only, no real Claude."""
import json
import shutil
from datetime import timedelta

import pytest
import yaml

from fetchers import yc_health, yc_repair, yc_scraper
from pipeline.claude_cli import ClaudeError
from tests.conftest import FIXTURES, NOW, ROOT

GOOD = (FIXTURES / "yc_results.html").read_text(encoding="utf-8")
BROKEN = (FIXTURES / "yc_results_broken.html").read_text(encoding="utf-8")
JOB = (FIXTURES / "yc_job.html").read_text(encoding="utf-8")
WAAS = (FIXTURES / "waas_results.html").read_text(encoding="utf-8")
COMPANY = (FIXTURES / "yc_company.html").read_text(encoding="utf-8")
WAAS_JOB = (FIXTURES / "waas_job.html").read_text(encoding="utf-8")
CFG = {"http": {"user_agent": "test"}, "yc": {"repair_model": "sonnet"}, "max_age_days": 14}


@pytest.fixture
def sel_path(tmp_path, monkeypatch):
    monkeypatch.delenv("YC_ENABLED", raising=False)
    p = tmp_path / "yc_selectors.yaml"
    shutil.copy(ROOT / "config" / "yc_selectors.yaml", p)
    return p


def rules(sel_path, name="yc_board"):
    return yc_scraper.page_rules(yc_scraper.load_selectors(sel_path), name)


def pages(board=GOOD, waas=WAAS):
    def load(url, ua):
        return (200, waas) if "workatastartup" in url else (200, board)
    return load


class Http:
    def __init__(self):
        self.calls = []

    def get(self, url, ttl_hours=None, as_json=False):
        self.calls.append(url)
        if "workatastartup.com/jobs/" in url:
            return WAAS_JOB
        if "/companies/" in url and "/jobs/" not in url:
            return COMPANY
        return JOB


def test_board_snapshot_extracts_expected_listings(sel_path):
    listings = yc_scraper.extract(GOOD, rules(sel_path))
    assert len(listings) == 8
    first = listings[0]
    assert (first["company"], first["title"], first["visa"]) == ("BlueCargo", "AI Engineer", "US citizen/visa only")


def test_waas_snapshot_extracts_listings_with_built_urls(sel_path):
    listings = yc_scraper.extract(WAAS, rules(sel_path, "waas_jobs"))
    assert len(listings) == 6
    assert listings[0]["company_slug"] == "givecampus"
    assert listings[0]["url"] == "https://www.workatastartup.com/jobs/112545"


def test_healthy_pages_pass_all_checks(sel_path):
    for name, html in (("yc_board", GOOD), ("waas_jobs", WAAS)):
        result = yc_health.check(200, html, rules(sel_path, name), recent_counts=[6, 7])
        assert result["status"] == "healthy", (name, result["checks"])


@pytest.mark.parametrize("status,html,recent,failing", [
    (200, BROKEN, [], "page_loads"),                 # container missing
    (500, "", [], "page_loads"),
    (302, GOOD, [], "page_loads"),                   # redirected (e.g. to a login page)
    (200, GOOD, [40, 40, 40], "listing_count"),      # 8 < 30% of the usual 40
])
def test_broken_pages(sel_path, status, html, recent, failing):
    result = yc_health.check(status, html, rules(sel_path), recent)
    assert result["status"] == "broken" and not result["checks"][failing]


def test_off_domain_url_is_broken(sel_path):
    html = GOOD.replace("/companies/bluecargo/jobs/", "https://evil.example/jobs/")
    result = yc_health.check(200, html, rules(sel_path), [])
    assert not result["checks"]["url_safety"] and result["status"] == "broken"


def test_parse_age():
    assert (NOW - yc_scraper.parse_age("24 days", NOW)).days == 24
    assert (NOW - yc_scraper.parse_age("about 2 months", NOW)).days == 60
    assert (NOW - yc_scraper.parse_age("over 1 year", NOW)).days == 366
    assert (NOW - yc_scraper.parse_age("5 hours", NOW)).days == 0
    assert yc_scraper.parse_age(None, NOW) is None


def test_board_detail_gives_description_team_size_and_verified_founders(sel_path):
    sel = rules(sel_path)
    detail = yc_scraper.parse_detail(JOB, sel)
    raw = yc_scraper.to_raw(yc_scraper.extract(GOOD, sel)[0], detail, NOW, 0)
    assert "Team size: 35 employees" in raw.description and "Visa: US citizen/visa only" in raw.description
    assert (NOW - raw.posted_at).days == 24
    people = yc_scraper.founders(detail)
    assert [p["name"] for p in people] == ["Alexandra Griffon", "Laura Theveniau"]
    assert all(p["source"] == "yc_page" and p["verified"] == 1 for p in people)


def test_waas_detail_dates_from_yc_company_page(sel_path):
    sel = rules(sel_path, "waas_jobs")
    listing = yc_scraper.extract(WAAS, sel)[0]
    http = Http()
    detail = yc_scraper._detail(sel, listing, http, NOW, 14)
    assert http.calls == ["https://www.ycombinator.com/companies/givecampus",
                          "https://www.workatastartup.com/jobs/112545"]
    raw = yc_scraper.to_raw(listing, detail, NOW, 0)
    assert (NOW - raw.posted_at).days == 3
    assert "Visa: US citizen/visa only" in raw.description and "Team size: 130 employees" in raw.description
    assert len(raw.description) > 300                     # job text from the WaaS job page
    assert yc_scraper.founders(detail)


def test_waas_stale_job_skips_job_page(sel_path):
    sel = rules(sel_path, "waas_jobs")
    listing = {"id": 999001, "company_slug": "givecampus", "url": "https://www.workatastartup.com/jobs/999001"}
    http = Http()
    yc_scraper._detail(sel, listing, http, NOW, 14)
    assert http.calls == ["https://www.ycombinator.com/companies/givecampus"]   # 2 months old: no job page


def test_fetch_both_pages_only_new_details_and_founders(conn, sel_path):
    http = Http()
    raws, info = yc_scraper.fetch(conn, http, NOW, cfg=CFG, load_page=pages(), selectors_path=sel_path)
    assert info["status"] == "healthy" and info["pages"]["yc_board"]["count"] == 8
    assert info["pages"]["waas_jobs"]["count"] == 6 and len(raws) == 14
    assert all("ycombinator.com" in c or "workatastartup.com" in c for c in http.calls)
    assert conn.execute("SELECT COUNT(*) FROM people WHERE source='yc_page' AND verified=1").fetchone()[0] >= 2
    for raw in raws:   # simulate ingest, then a second run fetches no detail pages
        conn.execute("INSERT OR IGNORE INTO jobs (job_key, company, company_norm, title, title_norm, sources, "
                     "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                     (f"src:yc:{raw.external_id}", raw.company, raw.company.lower(), raw.title, raw.title.lower(),
                      json.dumps([{"source": "yc", "external_id": raw.external_id, "url": raw.url}]),
                      "2026-10-24T06:00:00Z", "2026-10-24T06:00:00Z"))
    http.calls.clear()
    raws2, info2 = yc_scraper.fetch(conn, http, NOW + timedelta(days=1), cfg=CFG, load_page=pages(),
                                    selectors_path=sel_path)
    assert http.calls == [] and info2["new"] == 0 and len(raws2) == 14


def test_same_job_on_both_pages_read_once(conn, sel_path):
    board_ids = {str(l["id"]) for l in yc_scraper.extract(GOOD, rules(sel_path))}
    waas = WAAS.replace("112545", next(iter(board_ids)))            # same YC job id on both pages
    raws, _ = yc_scraper.fetch(conn, Http(), NOW, cfg=CFG, load_page=pages(waas=waas), selectors_path=sel_path)
    ids = [r.external_id for r in raws]
    assert len(ids) == len(set(ids)) == 13


def test_one_broken_page_does_not_stop_the_other(conn, sel_path, tmp_path):
    raws, info = yc_scraper.fetch(conn, Http(), NOW, cfg=CFG, load_page=pages(waas=BROKEN),
                                  claude=fake_claude(ClaudeError("nope")), selectors_path=sel_path,
                                  snapshots=tmp_path / "snap")
    assert info["status"] == "partial" and len(raws) == 8
    assert (tmp_path / "snap" / f"{NOW:%Y-%m-%d}-waas_jobs.html").exists()


GOOD_FIX = {"mode": "json", "json": {"container": "[data-page]", "attribute": "data-page",
                                     "listings_path": "props.jobPostings",
                                     "fields": {"id": "id", "title": "title", "url": "url", "company": "companyName",
                                                "location": "location", "created": "createdAt"}}}


def fake_claude(payload):
    def call(prompt, model, schema, retries, timeout):
        assert "<snapshot>" in prompt and "untrusted" in prompt
        if isinstance(payload, Exception):
            raise payload
        return schema.model_validate(payload), {"usage": {}}
    return call


def break_board_rules(sel_path):
    doc = yaml.safe_load(sel_path.read_text(encoding="utf-8"))
    doc["pages"][0]["json"]["listings_path"] = "props.oldName"     # site renamed it -> broken
    sel_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_broken_page_repair_accepted_with_backup(conn, sel_path, tmp_path):
    break_board_rules(sel_path)
    raws, info = yc_scraper.fetch(conn, Http(), NOW, cfg=CFG, load_page=pages(), claude=fake_claude(GOOD_FIX),
                                  selectors_path=sel_path, snapshots=tmp_path / "snap")
    assert info["pages"]["yc_board"]["status"] == "broken" and info["repair"]["yc_board"]["accepted"]
    assert not any(r.url.startswith("https://www.ycombinator.com/companies/bluecargo") for r in raws)  # skipped
    snap = tmp_path / "snap" / f"{NOW:%Y-%m-%d}-yc_board.html"
    assert snap.exists() and "<script" not in snap.read_text(encoding="utf-8")
    backups = list(sel_path.parent.glob("yc_selectors.*.bak.yaml"))
    assert len(backups) == 1 and "props.oldName" in backups[0].read_text(encoding="utf-8")
    doc = yc_scraper.load_selectors(sel_path)
    assert doc["pages"][0]["json"]["listings_path"] == "props.jobPostings"
    assert doc["pages"][0]["detail"]["kind"] == "yc_job"            # untouched parts kept
    assert doc["pages"][1]["name"] == "waas_jobs"                   # other page untouched


@pytest.mark.parametrize("payload", [
    {"mode": "json", "json": {**GOOD_FIX["json"], "fields": {"title": "title"}}},       # missing required fields
    {"mode": "json", "json": {**GOOD_FIX["json"], "listings_path": "props.nothing"}},    # extracts nothing
    {"mode": "json", "json": {**GOOD_FIX["json"], "container": "<script>alert(1)</script>"}},  # not a selector
    ClaudeError("bad json"),
])
def test_bad_repair_rejected_old_rules_kept_no_backup(conn, sel_path, tmp_path, payload):
    break_board_rules(sel_path)
    before = sel_path.read_text(encoding="utf-8")
    raws, info = yc_scraper.fetch(conn, Http(), NOW, cfg=CFG, load_page=pages(), claude=fake_claude(payload),
                                  selectors_path=sel_path, snapshots=tmp_path / "snap")
    assert not info["repair"]["yc_board"]["accepted"] and info["manual_fix_needed"]
    assert sel_path.read_text(encoding="utf-8") == before
    assert not list(sel_path.parent.glob("yc_selectors.*.bak.yaml"))


def test_off_domain_repair_rejected(conn, sel_path, tmp_path):
    break_board_rules(sel_path)
    evil = GOOD.replace("/companies/", "https://evil.example/companies/")
    raws, info = yc_scraper.fetch(conn, Http(), NOW, cfg=CFG, load_page=pages(board=evil),
                                  claude=fake_claude(GOOD_FIX), selectors_path=sel_path, snapshots=tmp_path / "snap")
    assert not info["repair"]["yc_board"]["accepted"]


def test_repair_at_most_once_a_week_per_page(conn, sel_path, tmp_path):
    calls = []

    def claude(*a, **k):
        calls.append(1)
        raise ClaudeError("nope")
    for day in range(3):
        yc_scraper.fetch(conn, Http(), NOW + timedelta(days=day), cfg=CFG, load_page=pages(board=BROKEN),
                         claude=claude, selectors_path=sel_path, snapshots=tmp_path / "snap")
    assert len(calls) == 1


def test_page_disabled_after_14_broken_days_then_whole_yc(conn, sel_path, tmp_path, monkeypatch):
    monkeypatch.setattr(yc_scraper, "ROOT", tmp_path)          # .env written under tmp
    claude = fake_claude(ClaudeError("nope"))
    for day in range(16):
        yc_scraper.fetch(conn, Http(), NOW + timedelta(days=day), cfg=CFG, load_page=pages(board=BROKEN),
                         claude=claude, selectors_path=sel_path, snapshots=tmp_path / "snap")
    doc = yc_scraper.load_selectors(sel_path)
    assert doc["pages"][0]["enabled"] is False and doc["pages"][1]["enabled"] is True
    assert not (tmp_path / ".env").exists()                      # the other page still works
    for day in range(16, 32):
        yc_scraper.fetch(conn, Http(), NOW + timedelta(days=day), cfg=CFG, load_page=pages(board=BROKEN, waas=BROKEN),
                         claude=claude, selectors_path=sel_path, snapshots=tmp_path / "snap")
    assert "YC_ENABLED=false" in (tmp_path / ".env").read_text(encoding="utf-8")
    raws, info = yc_scraper.fetch(conn, Http(), NOW + timedelta(days=40), cfg=CFG, load_page=pages(),
                                  selectors_path=sel_path)
    assert info["status"] == "disabled" and raws == []
    monkeypatch.delenv("YC_ENABLED", raising=False)


def test_snapshot_is_stripped():
    html = '<html><body onload="x()"><script>evil()</script><style>a{}</style><div onclick="y()">hi</div></body></html>'
    out = yc_health.strip_snapshot(html)
    assert "script" not in out and "onclick" not in out and "onload" not in out and "hi" in out
