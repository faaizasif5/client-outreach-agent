from datetime import timedelta

import pytest

from pipeline import freshness as f
from tests.conftest import NOW


def test_recent_source_date_is_fresh():
    age = f.assess(NOW - timedelta(days=3), "", NOW, 14)
    assert (age.status, age.source, age.estimated) == ("fresh", "source", False)


def test_old_source_date_is_stale():
    assert f.assess(NOW - timedelta(days=30), "", NOW, 14).status == "stale"


def test_45_day_old_job_first_seen_today_is_never_fresh():
    # The tool finding a job today says nothing about when it was posted.
    # assess() has no first_seen parameter on purpose; only the posted date counts.
    age = f.assess(NOW - timedelta(days=45), "Posted today!", NOW, 14)
    assert age.status == "stale"


def test_boundary_is_inclusive():
    assert f.assess(NOW - timedelta(days=14), "", NOW, 14).status == "fresh"
    assert f.assess(NOW - timedelta(days=14, hours=1), "", NOW, 14).status == "stale"


@pytest.mark.parametrize("text,days", [
    ("Posted 3 days ago", 3),
    ("posted 2 weeks ago", 14),
    ("Posted yesterday", 1),
    ("Posted today", 0),
    ("5d ago", 5),
    ("posted 30+ days ago", 30),
    ("Posted 1 month ago", 30),
    ("posted 6 hours ago", 0),
])
def test_relative_text(text, days):
    got = f.parse_relative(text, NOW)
    assert got is not None and (NOW - got).days == days


def test_relative_text_used_when_source_has_no_date():
    age = f.assess(None, "Senior Engineer. Posted 4 days ago.", NOW, 14)
    assert (age.status, age.source) == ("fresh", "relative_text")
    assert f.assess(None, "Posted 3 weeks ago", NOW, 14).status == "stale"


def test_ats_date_used_next():
    age = f.assess(None, "", NOW, 14, ats_date=lambda: NOW - timedelta(days=2))
    assert (age.status, age.source) == ("fresh", "ats")


def test_no_date_no_evidence_is_age_unknown():
    age = f.assess(None, "Great job, apply now", NOW, 14, ats_date=lambda: None)
    assert age.status == "age_unknown" and age.posted_at is None


def test_newest_first_estimate():
    last_run = NOW - timedelta(days=1)
    age = f.assess(None, "", NOW, 14, newest_first_since=last_run)
    assert (age.status, age.source, age.estimated) == ("fresh", "newest_first", True)
    # Evidence older than the window is not enough.
    assert f.assess(None, "", NOW, 14, newest_first_since=NOW - timedelta(days=20)).status == "stale"


def test_newest_first_evidence_requires_a_seen_listing_below():
    # feed order newest first; True = already seen on a previous run
    assert f.newest_first_evidence([False, False, True, False]) == {0, 1}
    assert f.newest_first_evidence([False, False]) == set()        # no baseline
    assert f.newest_first_evidence([True, False]) == set()          # below every seen item


def test_future_date_is_clamped():
    age = f.assess(NOW + timedelta(days=2), "", NOW, 14)
    assert age.status == "fresh" and age.posted_at == NOW


@pytest.mark.parametrize("text,closed", [
    ("FILLED - Acme | Backend Engineer | Remote", True),
    ("Edit: this position has been filled, thanks all", True),
    ("Update: the role is now closed.", True),
    ("We are no longer hiring for this role", True),
    ("We've filled the backend role", True),
    ("We're building a fulfilled future", False),
    ("Help us close deals faster", False),
])
def test_closed_text(cfg, text, closed):
    assert f.is_closed_text(text, cfg["eligibility"]["closed"]) is closed
