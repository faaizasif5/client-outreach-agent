"""Company, title and URL normalization (spec section 7)."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LEGAL_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "gmbh"}
YC_TAG = re.compile(r"[\(\[]\s*yc\s*[a-z]?\d{0,4}\s*[\)\]]", re.I)

TRACKING_PARAMS = {"ref", "source"}

# Hosts that never identify the hiring company (job boards, ATS, mail and link services).
GENERIC_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "bamboohr.com",
    "breezy.hr", "recruitee.com", "smartrecruiters.com", "jobvite.com", "rippling.com",
    "rippling-ats.com", "teamtailor.com", "personio.de", "personio.com", "workday.com",
    "myworkdayjobs.com", "icims.com", "applytojob.com", "jazzhr.com", "notion.site",
    "notion.so", "google.com", "forms.gle", "typeform.com", "airtable.com", "bit.ly",
    "calendly.com", "github.com", "gitlab.com", "linkedin.com", "wellfound.com", "angel.co",
    "ycombinator.com", "workatastartup.com", "remoteok.com", "remoteok.io",
    "weworkremotely.com", "gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
    "yahoo.com", "icloud.com", "proton.me", "protonmail.com", "fastmail.com", "hey.com",
    "medium.com", "substack.com", "youtube.com", "twitter.com", "x.com",
}
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def norm_company(name: str) -> str:
    s = YC_TAG.sub(" ", name or "").lower()
    s = re.sub(r"[^\w\s&+]", " ", s)
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def norm_title(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"\bsr\b\.?", "senior", s)
    s = re.sub(r"\bjr\b\.?", "junior", s)
    s = re.sub(r"\s[-–—]\s", " ", s)
    s = re.sub(r"[()\[\]{},/|:;!?\"']", " ", s)
    return " ".join(s.split())


def title_similarity(a: str, b: str) -> float:
    """Token-set Dice coefficient: word order and duplicates don't matter."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


ATS_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?[\w-]+/jobs/(\d+)", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_jid=(\d+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/[\w.-]+/([0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{36})", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/[\w-]+/j/([0-9A-Z]+)", re.I)),
]


def ats_key(url: str | None) -> str | None:
    if not url:
        return None
    for provider, pattern in ATS_PATTERNS:
        m = pattern.search(url)
        if m:
            return f"ats:{provider}:{m.group(1)}"
    return None


def ats_board(url: str | None) -> tuple[str, str, str] | None:
    """(provider, board_slug, job_id) for ATS API lookups."""
    if not url:
        return None
    for provider, pattern in [
        ("greenhouse", re.compile(r"greenhouse\.io/([\w-]+)/jobs/(\d+)", re.I)),
        ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", re.I)),
        ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", re.I)),
    ]:
        m = pattern.search(url)
        if m:
            return provider, m.group(1), m.group(2)
    return None


def registered_domain(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_of(value: str) -> str | None:
    if not value:
        return None
    if "@" in value and "://" not in value:
        return value.rsplit("@", 1)[1].strip().lower()
    host = urlsplit(value if "://" in value else f"https://{value}").hostname
    return host.lower() if host else None


def company_domain(value: str | None) -> str | None:
    """Registered domain of a URL or email, unless it belongs to a generic service."""
    host = host_of(value or "")
    if not host or "." not in host:
        return None
    domain = registered_domain(host)
    return None if domain in GENERIC_DOMAINS else domain


def is_generic_domain(value: str | None) -> bool:
    host = host_of(value or "")
    return bool(host) and registered_domain(host) in GENERIC_DOMAINS
