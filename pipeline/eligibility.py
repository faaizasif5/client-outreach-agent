"""Deterministic eligibility gate (spec section 9). Runs before any AI; AI never overrides it.

Patterns live in config.yaml (`eligibility:`); the decision order lives here."""
import re
from dataclasses import dataclass, field

from pipeline.contacts import Contacts, route

NEGATION = re.compile(r"\b(no|not|without|don't|doesn't|never|nor)\b[^.]{0,25}$", re.I)
LOCATIONISH = re.compile(
    r"remote|time ?zones?|located|based|resid|anywhere|location|overlap|hours|work from|"
    r"countr|region|worldwide|global|hire in|hiring in|open to candidates", re.I)
OFFSET_RANGE = re.compile(
    r"(?:utc|gmt)\s*([+\-−]\s*\d{1,2})(?::\d\d)?\s*(?:to|-|–|and|through)\s*(?:utc|gmt)?\s*([+\-−]\s*\d{1,2})", re.I)
OVERLAP = [
    re.compile(r"(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?(?:hours?|hrs?)\s+(?:of\s+)?(?:daily\s+)?overlap", re.I),
    re.compile(r"overlap(?:ping)?\s+(?:of\s+|by\s+)?(?:at least\s+)?(\d{1,2})\+?\s*(?:hours?|hrs?)", re.I),
]
TRAVEL_PCT = [
    re.compile(r"travel[^.%\n]{0,40}?(\d{1,3})\s*%", re.I),
    re.compile(r"(\d{1,3})\s*%\s*(?:of the time\s*)?travel", re.I),
]
TEAM_SIZE = [
    re.compile(r"\bteam of (\d[\d,]*)\b", re.I),
    re.compile(r"\b(\d[\d,]*)[- ]person (?:team|company|startup)\b", re.I),
    re.compile(r"\b(\d[\d,]*)\+?\s*(?:employees|staff|team members)\b", re.I),
    re.compile(r"\b(\d[\d,]*)\+? people (?:team|company|startup|strong|on the team)\b", re.I),
]
FULL_US_OVERLAP = 8
STRETCH_BUCKETS = ("remote_us_stretch", "us_auth_stretch", "explicit_us_only")


@dataclass
class Eligibility:
    eligibility: str                  # eligible | stretch | uncertain | ineligible
    reason: str
    bucket: str | None = None         # eligible | remote_us_stretch | us_auth_stretch | explicit_us_only
    route: str | None = None          # email | portal | network | skipped
    flags: list[str] = field(default_factory=list)


def parse_team_size(text: str) -> int | None:
    sizes = [int(m.group(1).replace(",", "")) for p in TEAM_SIZE for m in p.finditer(text or "")]
    return max(sizes) if sizes else None


def _any(patterns, text, negatable=False) -> bool:
    for p in patterns:
        for m in re.finditer(p, text or "", re.I | re.M):
            if negatable and NEGATION.search(text[max(0, m.start() - 40):m.start()]):
                continue
            return True
    return False


def _words(fragments: list[str]) -> str:
    return "|".join(f"(?:{f})" for f in fragments)


def _region_loc(fragments, text) -> bool:
    return bool(re.search(rf"(?<![\w])(?:{_words(fragments)})(?![\w])", text or "", re.I))


def _region_strict(fragments, text) -> bool:
    """Region words in description sentences count only in clearly restrictive phrasing."""
    w = _words(fragments)
    patterns = [
        rf"\b(?:only|must be|based in|located in|resid\w* in|living in|within|hire in|hiring in|open to)\b\W+(?:[\w-]+\W+){{0,4}}?(?:{w})(?![\w])",
        rf"(?<![\w])(?:{w})[- ](?:only|based|time ?zones?|hours|residents?)\b",
        rf"\bremote\W{{1,3}}\(?(?:{w})(?![\w])",
    ]
    return any(re.search(p, text or "", re.I) for p in patterns)


def _offset_range(text) -> tuple[int, int] | None:
    m = OFFSET_RANGE.search(text or "")
    if not m:
        return None
    a, b = (int(re.sub(r"\s", "", g).replace("−", "-")) for g in m.groups())
    return min(a, b), max(a, b)


def _required_overlap(text, p) -> int | None:
    hours = [int(m.group(1)) for rx in OVERLAP for m in rx.finditer(text or "")]
    if _any(p["full_us_hours"], text):
        hours.append(FULL_US_OVERLAP)
    return max(hours) if hours else None


def _travel_percent(text) -> int | None:
    found = [int(m.group(1)) for rx in TRAVEL_PCT for m in rx.finditer(text or "")]
    return max(found) if found else None


def evaluate(*, title: str, location_text: str | None, description: str, contacts: Contacts,
             team_size: int | None, age_status: str, closed: bool, cfg: dict,
             overlap_hours: int) -> Eligibility:
    p = cfg["eligibility"]
    loc = f"{location_text or ''} | {title or ''}"
    desc = description or ""
    everything = f"{loc}\n{desc}"
    region_desc = " . ".join(s for s in re.split(r"[.!?\n]+", desc) if LOCATIONISH.search(s))
    flags: list[str] = []

    def no(reason):
        return Eligibility("ineligible", reason, flags=flags)

    # ---- hard stops
    if closed:
        return no("closed")
    if age_status == "stale":
        return no("stale")
    if _any(p["regulated"], everything, negatable=True) or (
            _any(p["citizenship"], everything, negatable=True) and _any(p["regulated_context"], everything)):
        return no("regulated")
    if _any(p["no_contractors"], everything):
        return no("no_contractors")
    pct = _travel_percent(everything)
    if _any(p["travel_required"], everything, negatable=True) or (
            pct is not None and pct >= p["travel_threshold_percent"]):
        return no("travel")
    if pct is not None or _any(p["travel_occasional"], everything):
        flags.append("travel_occasional")
    overlap = _required_overlap(everything, p)
    if overlap is not None and overlap > overlap_hours:
        return no("timezone_overlap")

    # ---- location: onsite / hybrid / city
    remote_in_loc = _any(p["remote"], loc)
    remote = remote_in_loc or _any(p["remote"], desc)
    onsite = (_any(p["onsite_location"], loc) and not remote_in_loc) or \
        (_any(p["onsite_description"], desc) and not remote_in_loc)
    hybrid = (_any(p["hybrid_location"], loc) and not remote_in_loc) or \
        (_any(p["hybrid_description"], desc) and not remote_in_loc)
    not_remote = None
    if onsite:
        not_remote = "onsite"
    elif hybrid:
        not_remote = "hybrid"
    elif not remote and (location_text or "").strip():
        not_remote = "city_required"
    if not_remote:
        sponsor = _any(p["sponsorship"], everything) and not _any(p["no_sponsorship"], everything)
        if _any(p["relocation"], everything) and sponsor:
            return _finish(Eligibility("eligible", "relocation_sponsorship", "eligible", flags=flags),
                           contacts, age_status)
        return no(not_remote)

    # ---- region and work authorization
    worldwide_desc = [x for x in p["worldwide"] if "global" not in x]
    worldwide = _any(p["worldwide"], loc) or _any(worldwide_desc, region_desc)
    asia = _any(p["asia_ok"], loc) or _any(p["asia_ok"], region_desc)
    other = _region_loc(p["other_region_words"], loc) or _region_strict(p["other_region_words"], region_desc)
    us = _region_loc(p["us_words"], loc) or _region_strict(p["us_words"], region_desc)
    rng = _offset_range(f"{loc} {region_desc}")
    if rng:
        if rng[0] <= 5 <= rng[1]:
            asia = True
        else:
            other = True
    explicit = _any(p["explicit_us_only"], everything)
    auth = explicit or _any(p["us_auth"], everything, negatable=True)
    if not (worldwide or asia) and (us or not other) and _any(p["no_sponsorship"], everything) and (us or auth):
        auth = True

    if worldwide or asia:
        if auth:
            return Eligibility("uncertain", "conflicting_signals", flags=flags)
        reason = "worldwide" if worldwide else "asia_included"
        return _finish(Eligibility("eligible", reason, "eligible", flags=flags), contacts, age_status)

    if us or auth:
        if other and not us:
            return no("region_locked")
        bucket = "explicit_us_only" if explicit else "us_auth_stretch" if auth else "remote_us_stretch"
        if explicit and not cfg["pitch_explicit_us_only"]:
            return no("explicit_us_only_disabled")
        if team_size is not None and team_size > cfg["stretch_max_team_size"]:
            return no("work_auth_large_company" if auth else "stretch_large_company")
        return _finish(Eligibility("stretch", bucket, bucket, flags=flags), contacts, age_status)

    if other:
        return no("region_locked")
    if _any(p["contractor_friendly"], everything):
        return _finish(Eligibility("eligible", "contractor_friendly", "eligible", flags=flags),
                       contacts, age_status)
    return Eligibility("uncertain", "region_unknown", flags=flags)


def _finish(result: Eligibility, contacts: Contacts, age_status: str) -> Eligibility:
    """Apply the routing table; a missing application path is a hard stop. Unknown age keeps
    an otherwise-passing job on the uncertain list (never drafted automatically)."""
    route_name, reason = route(result.bucket, contacts)
    if reason:
        return Eligibility("ineligible", reason, result.bucket, "skipped", result.flags)
    result.route = route_name
    if age_status == "age_unknown":
        return Eligibility("uncertain", "age_unknown", result.bucket, route_name, result.flags)
    return result
