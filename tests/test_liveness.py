"""Freshness check #2 (spec section 8): liveness of shortlisted jobs right before drafting."""
import json
from datetime import timedelta

import requests

from pipeline import freshness as f
from tests.conftest import NOW

CLOSED = [r"\bpositions? (has been |have been |is |are |was |were )?(now )?filled\b", r"(?-i:\b(FILLED|CLOSED)\b)"]


class FakeHttp:
    """url -> (status, final_url, text) or an exception."""
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_status(self, url):
        self.calls.append(url)
        page = self.pages.get(url, (200, url, "Apply now"))
        if isinstance(page, Exception):
            raise page
        return page


def job(sources=("remoteok",), apply_url="https://acme.com/jobs/backend-engineer", last_seen=NOW):
    return {"job_key": "k", "apply_url": apply_url, "last_seen_at": last_seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": json.dumps([{"source": s, "external_id": "123", "url": f"https://{s}.test/123"}
                                   for s in sources])}


def check(j, pages, fetched=("remoteok",), complete=("wwr",)):
    return f.check_liveness(j, FakeHttp(pages), CLOSED, now=NOW, fetched_sources=set(fetched),
                            complete_feeds=set(complete))


def test_open_page():
    assert check(job(), {})[0] == "open"


def test_404_is_closed():
    url = "https://acme.com/jobs/backend-engineer"
    assert check(job(), {url: (404, url, "")}) == ("closed", "http_404")


def test_redirect_to_careers_home_is_closed():
    url = "https://acme.com/jobs/backend-engineer"
    assert check(job(), {url: (200, "https://acme.com/careers", "Join us")}) == ("closed", "redirected_home")


def test_closed_text_on_page():
    url = "https://acme.com/jobs/backend-engineer"
    assert check(job(), {url: (200, url, "Sorry, we are no longer accepting applications")})[0] == "closed"
    assert check(job(), {url: (200, url, "This position has been filled.")})[0] == "closed"


def test_network_error_is_unknown_not_open():
    url = "https://acme.com/jobs/backend-engineer"
    assert check(job(), {url: requests.ConnectionError("boom")})[0] == "unknown"
    assert check(job(), {url: (503, url, "")})[0] == "unknown"


def test_greenhouse_board_missing_posting_is_closed():
    j = job(apply_url="https://boards.greenhouse.io/acme/jobs/42")
    api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/42"
    assert check(j, {api: (404, api, "")}) == ("closed", "ats_missing")


def test_lever_missing_posting_is_closed():
    j = job(apply_url="https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555")
    api = "https://api.lever.co/v0/postings/acme/11111111-2222-3333-4444-555555555555"
    assert check(j, {api: (404, api, "")}) == ("closed", "ats_missing")


def test_ashby_board_without_job_is_closed():
    jid = "11111111-2222-3333-4444-555555555555"
    j = job(apply_url=f"https://jobs.ashbyhq.com/acme/{jid}")
    api = "https://api.ashbyhq.com/posting-api/job-board/acme"
    assert check(j, {api: (200, api, json.dumps({"jobs": [{"id": "other"}]}))}) == ("closed", "ats_missing")
    assert check(j, {api: (200, api, json.dumps({"jobs": [{"id": jid}]}))})[0] == "open"


def test_hn_comment_deleted_or_filled():
    j = job(sources=("hn",), apply_url=None)
    item = "https://hn.algolia.com/api/v1/items/123"
    assert check(j, {item: (200, item, json.dumps({"id": 123, "text": None, "children": []}))}) == ("closed", "hn_deleted")
    filled = {"id": 123, "author": "a", "text": "Acme | Backend", "children": [
        {"author": "a", "text": "Update: position filled, thanks!"}]}
    assert check(j, {item: (200, item, json.dumps(filled))}) == ("closed", "hn_filled")
    ok = {"id": 123, "author": "a", "text": "Acme | Backend", "children": []}
    assert check(j, {item: (200, item, json.dumps(ok))})[0] == "open"


def test_job_gone_from_complete_feed_is_closed():
    stale_seen = NOW - timedelta(days=1)
    j = job(sources=("wwr",), last_seen=stale_seen)
    assert check(j, {}, fetched=("wwr",)) == ("closed", "gone_from_feed")
    # RemoteOK's API only lists the latest jobs, so disappearing there proves nothing.
    assert check(job(sources=("remoteok",), last_seen=stale_seen), {}, fetched=("remoteok",))[0] == "open"
    # Feed not fetched this run -> no conclusion.
    assert check(j, {}, fetched=())[0] == "open"


def test_job_seen_this_run_is_not_gone_even_with_subsecond_now():
    now = NOW.replace(microsecond=654321)          # run clock has microseconds, DB has whole seconds
    j = job(sources=("wwr",), last_seen=NOW)
    status, _ = f.check_liveness(j, FakeHttp({}), CLOSED, now=now, fetched_sources={"wwr"},
                                 complete_feeds={"wwr"})
    assert status == "open"


def test_linkedin_never_fetched():
    j = job(apply_url="https://www.linkedin.com/jobs/view/1")
    http = FakeHttp({})
    status, _ = f.check_liveness(j, http, CLOSED, now=NOW, fetched_sources=set(), complete_feeds=set())
    assert all("linkedin.com" not in c for c in http.calls)
