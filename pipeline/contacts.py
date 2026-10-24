"""Contact extraction and routing (spec section 11). Recipients come only from the listing,
never from Claude. Routing is decided here, in code."""
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pipeline.normalize import canonical_url, company_domain, host_of, is_generic_domain, registered_domain

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
OBFUSCATED = [
    (re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.I), "."),
    (re.compile(r"\[\.\]"), "."),
    (re.compile(r"\[@\]"), "@"),
]
SPELLED = re.compile(r"\b([\w.+-]+)\s+at\s+([\w-]+(?:\s+dot\s+[\w-]+)+)\b", re.I)

IGNORED_LOCAL = {"support", "info", "sales", "privacy", "noreply", "no-reply", "donotreply",
                 "do-not-reply", "legal", "security", "abuse", "billing", "press", "media",
                 "marketing", "admin", "webmaster", "help", "feedback", "partners", "dpo"}
HIRING_LOCAL = {"jobs", "job", "careers", "career", "hiring", "recruiting", "recruitment",
                "recruiter", "talent", "hr", "apply", "join", "joinus", "work", "engineering-jobs"}
APPLY_CONTEXT = re.compile(
    r"(send|e-?mail|mail|apply|resume|\bcv\b|reach out|reach me|contact|write to|drop (me|us)|"
    r"interested\?|ping)", re.I)

ATS_HOSTS = ("ashbyhq.com", "greenhouse.io", "lever.co", "workable.com", "workatastartup.com",
             "bamboohr.com", "breezy.hr", "recruitee.com", "smartrecruiters.com", "jobvite.com",
             "rippling-ats.com", "teamtailor.com", "personio.de", "personio.com",
             "myworkdayjobs.com", "applytojob.com", "jazzhr.com", "wellfound.com")
CAREERS_PATH = re.compile(r"/(careers?|jobs?|join(-us)?|apply|positions|openings|work-with-us|hiring)(/|$)", re.I)

PORTAL_PREF = [
    r"\bapply (only )?(through|via|on|at|using) (our|the) (website|site|portal|careers? page|link|form|ats|job board)",
    r"\bno (emails|e-mails|email applications)\b",
    r"\b(please )?(do not|don't) (email|e-mail)\b",
    r"\bapplications (are )?only accepted (through|via|on)\b",
]
EMAIL_PREF = [
    r"\b(send|email|e-mail|mail) (us |me )?(your )?(resume|cv|application|portfolio)",
    r"\bapply (by|via|through) e-?mail\b",
    r"\b(email|e-mail|reach out to|contact) (us|me) (at|on)\b",
    r"\bapply:\s*\S+@",
]


@dataclass
class Contacts:
    recruiting_email: str | None = None
    apply_url: str | None = None
    founder_links: list[str] = field(default_factory=list)
    preference: str | None = None          # portal | email | None
    flags: list[str] = field(default_factory=list)


def _deobfuscate(text: str) -> str:
    for pattern, repl in OBFUSCATED:
        text = pattern.sub(repl, text)
    return SPELLED.sub(lambda m: f"{m.group(1)}@{re.sub(r'\s+dot\s+', '.', m.group(2), flags=re.I)}", text)


def extract_emails(text: str, links: list[str] = ()) -> list[str]:
    found = []
    for link in links:
        if link.lower().startswith("mailto:"):
            found.append(link[7:].split("?")[0])
    found += [m.group(0) for m in EMAIL_RE.finditer(_deobfuscate(text or ""))]
    out = []
    for e in found:
        e = e.strip().strip(".").lower()
        if e not in out:
            out.append(e)
    return out


def _email_contexts(text: str) -> dict[str, str]:
    """Up to 80 chars of text before each email's first appearance."""
    clean = _deobfuscate(text or "")
    ctx = {}
    for m in EMAIL_RE.finditer(clean):
        ctx.setdefault(m.group(0).strip(".").lower(), clean[max(0, m.start() - 80):m.start()])
    return ctx


def stated_preference(text: str) -> str | None:
    for p in PORTAL_PREF:
        if re.search(p, text or "", re.I):
            return "portal"
    for p in EMAIL_PREF:
        if re.search(p, text or "", re.I):
            return "email"
    return None


def is_apply_url(url: str) -> bool:
    host = host_of(url)
    if not host or not url.lower().startswith("http"):
        return False
    if any(host == h or host.endswith("." + h) for h in ATS_HOSTS):
        return True
    if "ycombinator.com" in host and "/jobs" in url:
        return True
    if is_generic_domain(url):
        return False
    return bool(CAREERS_PATH.search(urlsplit(url).path)) or host.startswith(("jobs.", "careers."))


def extract_contacts(text: str, links: list[str], *, source: str, company_domain: str | None,
                     explicit_apply_url: str | None = None) -> Contacts:
    result = Contacts(preference=stated_preference(text))
    contexts = _email_contexts(text)
    ranked = []
    for email in extract_emails(text, links):
        local, domain = email.split("@", 1)
        if local in IGNORED_LOCAL:
            continue
        hiring_style = local in HIRING_LOCAL
        in_context = bool(APPLY_CONTEXT.search(contexts.get(email, ""))) or email in (
            l[7:].split("?")[0].lower() for l in links if l.lower().startswith("mailto:"))
        if not (hiring_style or in_context):
            continue
        mismatch = bool(company_domain) and registered_domain(domain) != company_domain
        if mismatch and source != "hn":
            continue
        ranked.append((mismatch, not hiring_style, email))
    if ranked:
        mismatch, _, email = sorted(ranked, key=lambda r: (r[0], r[1]))[0]
        result.recruiting_email = email
        if mismatch:
            result.flags.append("email_domain_mismatch")
    if explicit_apply_url:
        result.apply_url = explicit_apply_url
    else:
        result.apply_url = next((l for l in links if is_apply_url(l)), None)
    for link in links:
        if re.search(r"linkedin\.com/in/[\w%-]+", link, re.I):
            canon = canonical_url(link.split("?")[0])
            if canon not in result.founder_links:
                result.founder_links.append(canon)
    return result


def email_company_domain(email: str | None) -> str | None:
    return company_domain(email) if email else None


def route(bucket: str, c: Contacts) -> tuple[str, str | None]:
    """(route, ineligible_reason). route: email | portal | network | skipped."""
    stretch = bucket != "eligible"
    email, url, founders = c.recruiting_email, c.apply_url, bool(c.founder_links)
    if not email and not url and not (stretch and founders):
        return "skipped", "no_application_path"
    if c.preference == "portal" and url:
        if not stretch:
            return "portal", None
        # Stretch never uses portals, and the listing asked for no emails.
        return ("network", None) if founders else ("skipped", "stretch_no_direct_contact")
    if email:
        if url and not stretch and c.preference != "email":
            return "portal", None   # the listing's own process wins
        return "email", None
    if not stretch:
        return "portal", None
    return ("network", None) if founders else ("skipped", "stretch_no_direct_contact")
