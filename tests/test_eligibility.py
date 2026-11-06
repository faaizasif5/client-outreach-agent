"""Eligibility gate (spec section 9). Covers every row of the example table plus edge cases."""
import copy

import pytest

from pipeline import eligibility as el
from pipeline.contacts import Contacts

EMAIL = "founder@acme.com"
URL = "https://jobs.ashbyhq.com/acme/1"
LI = ["https://linkedin.com/in/founder"]


def ev(cfg, desc="", loc=None, title="Software Engineer", email=None, url=URL, founders=(),
       age="fresh", closed=False, overlap=4, **cfg_overrides):
    cfg = copy.deepcopy(cfg)
    cfg.update(cfg_overrides)
    contacts = Contacts(recruiting_email=email, apply_url=url, founder_links=list(founders))
    return el.evaluate(title=title, location_text=loc, description=desc, contacts=contacts,
                       team_size=el.parse_team_size(desc), age_status=age, closed=closed,
                       cfg=cfg, overlap_hours=overlap)


def outcome(r):
    return (r.eligibility, r.bucket if r.eligibility == "stretch" else r.reason)


# ----- spec table rows

def test_worldwide_contractors_welcome(cfg):
    r = ev(cfg, "Contractors welcome.", loc="Remote (Worldwide)")
    assert (r.eligibility, r.bucket) == ("eligible", "eligible")
    assert r.route == "portal"


def test_us_auth_small_startup_with_founder_email(cfg):
    r = ev(cfg, "Remote — must be authorized to work in the US. We're an 8-person startup.",
           loc="Remote", email=EMAIL, url=None)
    assert outcome(r) == ("stretch", "us_auth_stretch") and r.route == "email"


def test_us_auth_large_company(cfg):
    r = ev(cfg, "Remote — must be authorized to work in the US. Join our 500-person company.",
           loc="Remote", email=EMAIL)
    assert outcome(r) == ("ineligible", "work_auth_large_company")


def test_regulated(cfg):
    r = ev(cfg, "US citizens only, federal contracts / clearance required.", loc="Remote (US)",
           email=EMAIL)
    assert outcome(r) == ("ineligible", "regulated")


def test_explicit_us_only_enabled(cfg):
    r = ev(cfg, "US-based only. Please do not apply from outside the US. Team of 6.",
           loc="Remote", email=EMAIL)
    assert outcome(r) == ("stretch", "explicit_us_only") and r.route == "email"


def test_explicit_us_only_disabled(cfg):
    r = ev(cfg, "US-based only. Please do not apply from outside the US. Team of 6.",
           loc="Remote", email=EMAIL, pitch_explicit_us_only=False)
    assert outcome(r) == ("ineligible", "explicit_us_only_disabled")


def test_us_citizen_small_startup_founder_linkedin_goes_to_network(cfg):
    r = ev(cfg, "Must be US citizen. We are a 15-person startup.", loc="Remote",
           url="https://www.workatastartup.com/jobs/1", founders=LI)
    assert outcome(r) == ("stretch", "us_auth_stretch") and r.route == "network"


def test_us_auth_300_person_company(cfg):
    r = ev(cfg, "Must be authorized to work in the US. We have 300 employees.", loc="Remote",
           email=EMAIL)
    assert outcome(r) == ("ineligible", "work_auth_large_company")


def test_fde_travel_30_percent(cfg):
    r = ev(cfg, "Remote, travel up to 30%.", loc="Remote (Worldwide)",
           title="Forward Deployed Engineer")
    assert outcome(r) == ("ineligible", "travel")


def test_fde_occasional_travel_worldwide(cfg):
    r = ev(cfg, "Remote, occasional travel.", loc="Remote, worldwide",
           title="Forward Deployed Engineer")
    assert r.eligibility == "eligible" and "travel_occasional" in r.flags


def test_remote_us_small_team_founder_email(cfg):
    r = ev(cfg, "Small team of 5.", loc="Remote (US)", email=EMAIL, url=None)
    assert outcome(r) == ("stretch", "remote_us_stretch") and r.route == "email"


def test_remote_us_portal_only(cfg):
    r = ev(cfg, "", loc="Remote (US)", email=None, url=URL)
    assert outcome(r) == ("ineligible", "stretch_no_direct_contact")


def test_remote_no_region(cfg):
    assert outcome(ev(cfg, "", loc="Remote")) == ("uncertain", "region_unknown")


def test_eu_time_zones_only(cfg):
    assert outcome(ev(cfg, "", loc="Remote, EU time zones only")) == ("ineligible", "region_locked")


def test_onsite_sf(cfg):
    assert outcome(ev(cfg, "", loc="Onsite, San Francisco")) == ("ineligible", "onsite")


def test_stale(cfg):
    assert outcome(ev(cfg, "", loc="Remote (Worldwide)", age="stale")) == ("ineligible", "stale")


def test_closed(cfg):
    assert outcome(ev(cfg, "", loc="Remote (Worldwide)", closed=True)) == ("ineligible", "closed")


# ----- more edge cases

def test_age_unknown_is_uncertain_never_eligible(cfg):
    assert outcome(ev(cfg, "", loc="Remote (Worldwide)", age="age_unknown")) == ("uncertain", "age_unknown")


def test_age_unknown_does_not_hide_a_hard_stop(cfg):
    assert outcome(ev(cfg, "", loc="Onsite, NYC", age="age_unknown")) == ("ineligible", "onsite")


@pytest.mark.parametrize("loc,desc", [
    ("Remote - Canada", ""),
    ("Remote (UK only)", ""),
    ("Remote", "You must be based in Europe."),
    ("Remote (India)", ""),
    ("Remote, UTC-3 to UTC+3", ""),
])
def test_region_locked(cfg, loc, desc):
    assert outcome(ev(cfg, desc, loc=loc)) == ("ineligible", "region_locked")


@pytest.mark.parametrize("loc,desc", [
    ("Remote (Pakistan)", ""),
    ("Remote - APAC", ""),
    ("Remote", "We hire in Asia time zones."),
    ("Remote, UTC+3 to UTC+8", ""),
    ("Anywhere", ""),
    ("Remote (Europe or Asia)", ""),
    ("Remote", "Work from anywhere in the world."),
])
def test_eligible_regions(cfg, loc, desc):
    assert ev(cfg, desc, loc=loc).eligibility == "eligible"


def test_anywhere_in_the_us_is_us(cfg):
    r = ev(cfg, "Team of 4.", loc="Remote, anywhere in the US", email=EMAIL)
    assert outcome(r) == ("stretch", "remote_us_stretch")


def test_us_time_zones_is_remote_us_stretch(cfg):
    r = ev(cfg, "", loc="Remote (US time zones)", email=EMAIL)
    assert outcome(r) == ("stretch", "remote_us_stretch")


def test_required_overlap_above_profile_is_ineligible(cfg):
    r = ev(cfg, "Must have 6 hours of overlap with PST.", loc="Remote (Worldwide)")
    assert outcome(r) == ("ineligible", "timezone_overlap")
    assert ev(cfg, "Need 3 hours overlap with EST.", loc="Remote (Worldwide)").eligibility == "eligible"


def test_full_us_hours_is_ineligible(cfg):
    r = ev(cfg, "You will work PST business hours.", loc="Remote (US)", email=EMAIL)
    assert outcome(r) == ("ineligible", "timezone_overlap")


def test_negated_clearance_is_not_regulated(cfg):
    r = ev(cfg, "No security clearance required.", loc="Remote (Worldwide)")
    assert r.eligibility == "eligible"


@pytest.mark.parametrize("desc", ["No contractors.", "This is a W2 only position.",
                                  "Must be a W-2 employee."])
def test_no_contractors(cfg, desc):
    assert outcome(ev(cfg, desc, loc="Remote (Worldwide)")) == ("ineligible", "no_contractors")


def test_hybrid(cfg):
    assert outcome(ev(cfg, "", loc="Hybrid (NYC)")) == ("ineligible", "hybrid")


def test_city_only(cfg):
    assert outcome(ev(cfg, "", loc="San Francisco, CA")) == ("ineligible", "city_required")


def test_onsite_with_relocation_and_sponsorship(cfg):
    r = ev(cfg, "We offer relocation and visa sponsorship.", loc="Onsite, Berlin")
    assert r.eligibility == "eligible" and r.reason == "relocation_sponsorship"


def test_remote_or_onsite_counts_as_remote(cfg):
    assert ev(cfg, "", loc="Remote or Onsite (SF), worldwide").eligibility == "eligible"


def test_worldwide_with_us_auth_conflicts(cfg):
    r = ev(cfg, "Must be authorized to work in the US.", loc="Remote (Worldwide)")
    assert outcome(r) == ("uncertain", "conflicting_signals")


def test_no_sponsorship_on_worldwide_role_is_not_us_auth(cfg):
    assert ev(cfg, "No visa sponsorship.", loc="Remote (Worldwide)").eligibility == "eligible"


def test_contractor_friendly_without_region(cfg):
    r = ev(cfg, "We pay contractors via Deel.", loc="Remote")
    assert (r.eligibility, r.reason) == ("eligible", "contractor_friendly")


def test_no_application_path(cfg):
    r = ev(cfg, "", loc="Remote (Worldwide)", email=None, url=None)
    assert outcome(r) == ("ineligible", "no_application_path")


def test_travel_low_percent_is_allowed_and_flagged(cfg):
    r = ev(cfg, "Travel up to 10%.", loc="Remote (Worldwide)")
    assert r.eligibility == "eligible" and "travel_occasional" in r.flags


def test_on_site_with_customers_is_travel(cfg):
    r = ev(cfg, "You'll spend weeks on-site with customers.", loc="Remote (Worldwide)")
    assert outcome(r) == ("ineligible", "travel")


def test_unknown_team_size_allowed_for_stretch(cfg):
    r = ev(cfg, "Must be authorized to work in the US.", loc="Remote", email=EMAIL)
    assert outcome(r) == ("stretch", "us_auth_stretch")


def test_join_us_is_not_a_us_region(cfg):
    assert outcome(ev(cfg, "Come join US!", loc="Remote")) == ("uncertain", "region_unknown")


@pytest.mark.parametrize("text,size", [
    ("We're an 8-person startup", 8), ("team of 12 engineers", 12),
    ("over 1,200 employees", 1200), ("Join our 500-person company", 500),
    ("We help 2 million people", None), ("", None),
])
def test_parse_team_size(text, size):
    assert el.parse_team_size(text) == size


@pytest.mark.parametrize("visa,expected", [
    ("Visa: US Citizenship/Visa Not Required", ("stretch", "remote_us_stretch")),  # no auth requirement
    ("Visa: US citizen/visa only", ("stretch", "us_auth_stretch")),
    ("Visa: Will Sponsor", ("stretch", "remote_us_stretch")),
])
def test_yc_visa_field(cfg, visa, expected):
    r = ev(cfg, f"Location: San Francisco, CA, US / Remote\n{visa}\nTeam size: 3 employees",
           loc="San Francisco, CA, US / Remote", email=EMAIL)
    assert outcome(r) == expected


def test_remote_asia_beats_us_visa_label(cfg):
    loc = "San Francisco / Singapore / Remote (Asia) / Remote (North America)"
    r = ev(cfg, f"Location: {loc}\nVisa: US citizen/visa only", loc=loc)
    assert (r.eligibility, r.reason) == ("eligible", "asia_included") and "us_visa_label_ignored" in r.flags
