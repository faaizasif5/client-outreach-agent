"""Network tab people sources (spec section 11). linkedin.com is never fetched."""
from db import repo
from pipeline import people
from tests.conftest import NOW, make_job

TEAM_HTML = """
<html><body>
<div class="person"><h3>Sara Khan</h3><p>Co-founder &amp; CEO</p>
  <a href="https://www.linkedin.com/in/sara-khan/">LinkedIn</a></div>
<div class="person"><h3>Omar Ali</h3><p>Head of Engineering</p>
  <a href="https://linkedin.com/in/omar-ali">in</a></div>
<div class="person"><h3>Zoe Park</h3><p>Designer</p><a href="https://linkedin.com/in/zoe">in</a></div>
<div class="person"><h3>Lee Wong</h3><p>CTO</p><a href="https://linkedin.com/in/lee-wong">in</a></div>
</body></html>"""


def test_team_page_founders_first_max_two():
    found = people.from_team_page(TEAM_HTML, "https://acme.com/team")
    assert [(p["name"], p["source"], p["verified"]) for p in found] == [
        ("Sara Khan", "team_page", 1), ("Lee Wong", "team_page", 1)]
    assert found[0]["linkedin_url"] == "https://www.linkedin.com/in/sara-khan"


def test_signed_hn_post_gives_search_link():
    text = "Acme | Backend | Remote\nWe build billing APIs.\nI'm Jane Doe, co-founder and CTO. Email me."
    found = people.from_listing(text, [], "Acme")
    assert found[0]["name"] == "Jane Doe" and found[0]["source"] == "search" and found[0]["verified"] == 0
    assert found[0]["linkedin_url"].startswith("https://www.linkedin.com/search/results/people/?keywords=Jane+Doe+Acme")


def test_listing_profile_link_kept_unverified():
    found = people.from_listing("", ["https://www.linkedin.com/in/sam-f"], "Rocketship")
    assert found[0]["source"] == "listing" and found[0]["verified"] == 0


class Http:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch_status(self, url):
        self.calls.append(url)
        return self.pages.get(url, (404, url, ""))


def test_team_page_fetched_once_per_company(conn):
    http = Http({"https://acme.com/team": (200, "https://acme.com/team", TEAM_HTML)})
    assert people.fetch_team_page(conn, http, "acme", "acme.com", NOW) == 2
    calls = len(http.calls)
    assert people.fetch_team_page(conn, http, "acme", "acme.com", NOW) == 0
    assert len(http.calls) == calls                                 # never re-fetched
    assert all("linkedin.com" not in c for c in http.calls)
    assert set(people.verified_profile_links(conn, "acme")) == {
        "https://www.linkedin.com/in/sara-khan", "https://linkedin.com/in/lee-wong"}


def test_note_budget_counts_only_requested_with_note(conn):
    make_job(conn, job_key="j1")
    conn.execute("INSERT INTO people (company_norm, name, source, verified, fetched_at) VALUES ('acme','A','team_page',1,?)",
                 (repo.iso(NOW),))
    pid = conn.execute("SELECT id FROM people").fetchone()[0]
    for n, (status, note) in enumerate([("requested", "Hi"), ("requested", None), ("to_connect", "Hi"),
                                        ("connected", "Hi")]):
        conn.execute("INSERT INTO network (id, job_key, person_id, connection_note, status, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?)", (f"j1-{n}", "j1", pid, note, status, repo.iso(NOW), repo.iso(NOW)))
    assert people.notes_used_this_month(conn, NOW) == 3   # planned to_connect notes count too
