import pytest

from pipeline import normalize as n


@pytest.mark.parametrize("raw,expected", [
    ("Acme, Inc.", "acme"),
    ("Acme Inc", "acme"),
    ("Acme Co.", "acme"),
    ("Globex Corp", "globex"),
    ("Initech LLC", "initech"),
    ("Hooli Ltd.", "hooli"),
    ("Qux GmbH", "qux"),
    ("Baz (YC S23)", "baz"),
    ("Baz [YC W21]", "baz"),
    ("Foo Labs", "foo labs"),              # brand words are never stripped
    ("Foo AI", "foo ai"),
    ("Bar Technologies Ltd", "bar technologies"),
    ("  Zeta!  ", "zeta"),
    ("Co", "co"),                          # never strip down to nothing
])
def test_norm_company(raw, expected):
    assert n.norm_company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("https://Example.COM/jobs/123/?utm_source=hn&utm_medium=x&ref=abc", "https://example.com/jobs/123"),
    ("https://example.com/jobs/123?source=remoteok#apply", "https://example.com/jobs/123"),
    ("https://acme.com/careers?gh_jid=4455&utm_campaign=x", "https://acme.com/careers?gh_jid=4455"),
    ("http://www.acme.com/", "http://www.acme.com"),
])
def test_canonical_url(raw, expected):
    assert n.canonical_url(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Sr. Backend Engineer", "senior backend engineer"),
    ("Sr Backend   Engineer", "senior backend engineer"),
    ("Backend Engineer (Senior)", "backend engineer senior"),
    ("Jr. Full-Stack Developer", "junior full-stack developer"),
    ("C# / .NET Engineer", "c# .net engineer"),
])
def test_norm_title(raw, expected):
    assert n.norm_title(raw) == expected


def test_title_similarity():
    assert n.title_similarity("senior backend engineer", "backend engineer senior") >= 0.85
    assert n.title_similarity("backend engineer", "frontend engineer") < 0.85
    assert n.title_similarity("senior backend engineer", "backend engineer") < 0.85


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/4012345", "ats:greenhouse:4012345"),
    ("https://job-boards.greenhouse.io/acme/jobs/4012345?gh_src=x", "ats:greenhouse:4012345"),
    ("https://acme.com/careers?gh_jid=4012345", "ats:greenhouse:4012345"),
    ("https://jobs.lever.co/acme/1a2b3c4d-1111-2222-3333-444455556666/apply", "ats:lever:1a2b3c4d-1111-2222-3333-444455556666"),
    ("https://jobs.ashbyhq.com/acme/9f8e7d6c-aaaa-bbbb-cccc-ddddeeeeffff", "ats:ashby:9f8e7d6c-aaaa-bbbb-cccc-ddddeeeeffff"),
    ("https://apply.workable.com/acme/j/AB12CD34EF/", "ats:workable:AB12CD34EF"),
    ("https://acme.com/jobs/backend", None),
])
def test_ats_key(url, expected):
    assert n.ats_key(url) == expected


@pytest.mark.parametrize("value,expected", [
    ("https://careers.acme.com/jobs/1", "acme.com"),
    ("https://www.acme.co.uk/about", "acme.co.uk"),
    ("jobs@acme.io", "acme.io"),
    ("https://jobs.lever.co/acme/123", None),     # ATS host is not the company's domain
    ("https://boards.greenhouse.io/acme", None),
    ("someone@gmail.com", None),
    ("https://remoteok.com/remote-jobs/123", None),
    ("https://news.ycombinator.com/item?id=1", None),
])
def test_company_domain(value, expected):
    assert n.company_domain(value) == expected
