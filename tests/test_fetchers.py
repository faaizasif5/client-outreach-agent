import json
from datetime import datetime, timezone

import pytest

from fetchers import hn_whos_hiring as hn, remoteok, wwr
from fetchers.detail_fetch import BlockedHost, Http, html_to_text
from tests.conftest import FIXTURES

HTTP_CFG = {"user_agent": "test", "min_delay_seconds": 2.0, "max_delay_seconds": 3.0,
            "cache_ttl_hours": 6, "timeout_seconds": 5, "blocked_hosts": ["linkedin.com"]}


class FakeResp:
    def __init__(self, text):
        self.text, self.encoding = text, "utf-8"

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, body="{}"):
        self.headers, self.calls, self.body = {}, [], body

    def get(self, url, timeout):
        self.calls.append(url)
        return FakeResp(self.body)


def test_linkedin_is_never_requested(tmp_path):
    session = FakeSession()
    http = Http(HTTP_CFG, tmp_path, session=session)
    for url in ["https://www.linkedin.com/in/someone", "https://linkedin.com/company/x"]:
        with pytest.raises(BlockedHost):
            http.get(url)
    assert session.calls == []


def test_cache_and_polite_delay(tmp_path):
    session, sleeps = FakeSession('{"a": 1}'), []
    clock = iter([0.0, 0.5, 0.5]).__next__
    http = Http(HTTP_CFG, tmp_path, session=session, sleep=sleeps.append, clock=clock)
    assert http.get("https://example.com/a", as_json=True) == {"a": 1}
    assert http.get("https://example.com/a", as_json=True) == {"a": 1}   # cache hit
    http.get("https://example.com/b")
    assert len(session.calls) == 2
    assert len(sleeps) == 1 and 1.5 <= sleeps[0] <= 2.5   # waited the rest of a 2-3 s gap


def test_html_to_text():
    text, links = html_to_text('Hi<p>Apply <a href="https://x.test/jobs">here</a><br>now')
    assert text.splitlines() == ["Hi", "Apply here", "now"]
    assert links == ["https://x.test/jobs"]


def test_hn_current_month_thread_only():
    search = json.loads((FIXTURES / "hn_search.json").read_text(encoding="utf-8"))
    assert hn.current_thread_id(search, datetime(2026, 10, 24)) == "8000"
    assert hn.current_thread_id(search, datetime(2026, 11, 1)) is None   # not posted yet


def test_hn_parse_thread():
    thread = json.loads((FIXTURES / "hn_thread.json").read_text(encoding="utf-8"))
    jobs = {j.external_id: j for j in hn.parse_thread(thread)}
    assert "9011" not in jobs                       # deleted comment skipped
    acme = jobs["9001"]
    assert acme.company == "Acme Labs"
    assert acme.title == "Senior Backend Engineer (Django, Postgres)"
    assert "REMOTE (Worldwide)" in acme.location_text
    assert acme.posted_at == datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    assert acme.company_url == "https://acme.dev"
    assert "https://jobs.ashbyhq.com/acmelabs/11111111-2222-3333-4444-555555555555" in acme.links
    assert jobs["9002"].company == "Rocketship (YC W25)"
    assert "filled" in jobs["9006"].replies_text


def test_remoteok_parse_skips_legal_notice():
    data = json.loads((FIXTURES / "remoteok.json").read_text(encoding="utf-8"))
    jobs = remoteok.parse(data)
    assert [j.external_id for j in jobs] == ["501", "502", "503", "504", "505"]
    assert jobs[0].location_text == "Remote (Worldwide)" and jobs[2].location_text == "Remote"
    assert jobs[1].company == "Ledger & Co"
    assert jobs[4].posted_at is None
    assert jobs[0].feed_rank == 0


def test_wwr_parse():
    jobs = wwr.parse((FIXTURES / "wwr.rss").read_text(encoding="utf-8"))
    assert [(j.company, j.title) for j in jobs][:2] == [("Cloudy", "Full Stack Engineer"),
                                                        ("Statesider", "Backend Developer")]
    assert jobs[0].location_text == "Remote (Anywhere in the World)"
    assert jobs[0].external_id == "cloudy-full-stack-engineer"
    assert jobs[0].posted_at == datetime(2026, 10, 23, 7, 31, 3, tzinfo=timezone.utc)
