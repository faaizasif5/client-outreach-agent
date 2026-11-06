import json
from datetime import timedelta

from pipeline import dedupe
from pipeline.dedupe import Candidate
from tests.conftest import NOW


def cand(source="hn", external_id="100", company="Acme", title="Backend Engineer",
         domain=None, url=None, apply_url=None, posted_at=None, **fields):
    from pipeline import normalize as n
    url = url or f"https://{source}.test/item/{external_id}"
    return Candidate(
        source=source, external_id=external_id, source_url=url, company=company,
        company_norm=n.norm_company(company), company_domain=domain, title=title,
        title_norm=n.norm_title(title), canonical_url=n.canonical_url(url),
        ats_key=n.ats_key(apply_url), apply_url=n.canonical_url(apply_url),
        posted_at=posted_at, fields=fields)


def rows(conn):
    return conn.execute("SELECT * FROM jobs ORDER BY first_seen_at, job_key").fetchall()


def test_job_key_strength():
    gh = "https://boards.greenhouse.io/acme/jobs/42"
    assert dedupe.job_key_for(cand(apply_url=gh)) == "ats:greenhouse:42"
    assert dedupe.job_key_for(cand(external_id="7")) == "src:hn:7"
    k = dedupe.job_key_for(cand(external_id=""))
    assert k.startswith("sha1:") and len(k) == 45


def test_same_ats_id_on_two_sources_is_one_row(conn):
    gh = "https://boards.greenhouse.io/acme/jobs/42"
    k1, new1, _ = dedupe.upsert(conn, cand("hn", "1", apply_url=gh), NOW)
    k2, new2, _ = dedupe.upsert(conn, cand("remoteok", "999", title="Sr. Backend Eng",
                                           apply_url=gh + "?utm_source=remoteok"), NOW)
    assert (k1, new1, new2) == (k2, True, False)
    assert len(rows(conn)) == 1
    sources = json.loads(rows(conn)[0]["sources"])
    assert {s["source"] for s in sources} == {"hn", "remoteok"}


def test_same_source_external_id_is_same_job(conn):
    dedupe.upsert(conn, cand("remoteok", "5"), NOW)
    _, new, _ = dedupe.upsert(conn, cand("remoteok", "5"), NOW + timedelta(days=1))
    assert not new and len(rows(conn)) == 1
    assert rows(conn)[0]["last_seen_at"] == "2026-10-25T06:00:00Z"
    assert rows(conn)[0]["first_seen_at"] == "2026-10-24T06:00:00Z"


def test_same_specific_job_url_is_same_job(conn):
    dedupe.upsert(conn, cand("hn", "1", apply_url="https://acme.com/careers/backend-engineer"), NOW)
    _, new, _ = dedupe.upsert(conn, cand("wwr", "x", company="ACME Inc.", title="Platform Eng",
                                         apply_url="https://acme.com/careers/backend-engineer/"), NOW)
    assert not new and len(rows(conn)) == 1


def test_generic_careers_page_does_not_merge_different_roles(conn):
    dedupe.upsert(conn, cand("hn", "1", title="Backend Engineer", apply_url="https://acme.com/careers"), NOW)
    dedupe.upsert(conn, cand("wwr", "2", title="Designer", apply_url="https://acme.com/careers"), NOW)
    assert len(rows(conn)) == 2


def test_same_company_same_title_cross_source_merges(conn):
    dedupe.upsert(conn, cand("hn", "1", company="Acme Labs", title="Backend Engineer"), NOW)
    _, new, _ = dedupe.upsert(conn, cand("wwr", "w1", company="Acme Labs, Inc.",
                                         title="Backend  Engineer"), NOW)
    assert not new and len(rows(conn)) == 1


def test_same_company_same_title_different_ats_ids_are_separate(conn):
    dedupe.upsert(conn, cand("hn", "1", apply_url="https://boards.greenhouse.io/acme/jobs/1"), NOW)
    _, new, flags = dedupe.upsert(conn, cand("hn", "2", apply_url="https://boards.greenhouse.io/acme/jobs/2"), NOW)
    assert new and len(rows(conn)) == 2
    assert flags == ["possible_repost:ats:greenhouse:1"]


def test_repost_with_new_source_id_is_separate_but_flagged(conn):
    dedupe.upsert(conn, cand("remoteok", "100", title="Senior Backend Engineer"), NOW)
    _, new, flags = dedupe.upsert(conn, cand("remoteok", "200", title="Senior Backend Engineer"), NOW)
    assert new and flags == ["possible_repost:src:remoteok:100"]
    stored = json.loads(rows(conn)[1]["flags"])
    assert stored == ["possible_repost:src:remoteok:100"]


def test_similar_title_kept_separate_flagged(conn):
    dedupe.upsert(conn, cand("hn", "1", title="Senior Backend Engineer"), NOW)
    _, new, flags = dedupe.upsert(conn, cand("wwr", "w9", title="Backend Engineer (Senior)"), NOW)
    assert new and flags == ["possible_repost:src:hn:1"]


def test_labs_kept_so_different_companies_stay_separate(conn):
    dedupe.upsert(conn, cand("hn", "1", company="Foo Labs", title="Backend Engineer"), NOW)
    _, new, flags = dedupe.upsert(conn, cand("wwr", "2", company="Foo", title="Backend Engineer"), NOW)
    assert new and flags == []


def test_domain_identity_merges_different_names(conn):
    dedupe.upsert(conn, cand("hn", "1", company="Acme", domain="acme.io"), NOW)
    _, new, _ = dedupe.upsert(conn, cand("wwr", "2", company="Acme Robotics", domain="acme.io"), NOW)
    assert not new


def test_merge_keeps_earliest_posted_date_and_fills_gaps(conn):
    old = NOW - timedelta(days=20)
    dedupe.upsert(conn, cand("hn", "1", posted_at=old, contact_email=None), NOW)
    dedupe.upsert(conn, cand("wwr", "2", posted_at=NOW, contact_email="jobs@acme.com",
                             domain="acme.com"), NOW)
    r = rows(conn)[0]
    assert r["posted_at"] == "2026-10-04T06:00:00Z"   # a repost never makes an old job fresh
    assert r["contact_email"] == "jobs@acme.com" and r["company_domain"] == "acme.com"
