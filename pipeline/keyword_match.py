"""Keyword pre-match: decides which eligible/stretch jobs are worth a scoring slot (max 30)."""
import re


def _hit(pattern: str, text: str) -> bool:
    if re.fullmatch(r"[\w .+#-]+", pattern):   # plain keyword: match on word edges
        return re.search(rf"(?<![\w]){re.escape(pattern)}(?![\w])", text, re.I) is not None
    return re.search(pattern, text, re.I) is not None


def is_engineering_role(title: str, cfg: dict) -> bool:
    """True if any piece of a (possibly multi-role) title is an IC engineering role."""
    kw = cfg["keywords"]
    for piece in re.split(r"[,/;|&+]| and ", title or ""):
        if any(_hit(p, piece) for p in kw["role_title_include"]) and \
                not any(_hit(p, piece) for p in kw["role_title_exclude"]):
            return True
    return False


def profile_keywords(profile: dict) -> set[str]:
    words = set()
    for resume in (profile.get("resumes") or {}).values():
        if isinstance(resume, dict):
            words.update(k.lower() for k in resume.get("keywords") or [])
    skills = profile.get("skills") or {}
    for group in ("production", "projects"):
        words.update(s.lower() for s in skills.get(group) or [])
    return words


def keyword_score(title: str, description: str, profile: dict, cfg: dict) -> float:
    words = profile_keywords(profile)
    title_hits = sum(1 for w in words if _hit(w, title or ""))
    desc_hits = sum(1 for w in words if _hit(w, description or ""))
    boost = sum(1 for p in cfg["keywords"]["role_title_boost"] if _hit(p, title or ""))
    return 3 * title_hits + desc_hits + 2 * boost
