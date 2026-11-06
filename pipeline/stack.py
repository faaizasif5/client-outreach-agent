"""Stack matching: what a listing asks for, what the candidate has, and what a draft may name.
Code decides this; Claude is told which technologies to lean on and which never to claim."""
import re
from dataclasses import dataclass, field

TIER_ORDER = ("production", "projects", "other")


def _patterns(cfg: dict) -> dict[str, list[re.Pattern]]:
    return {tech: [re.compile(a, re.I) for a in aliases] for tech, aliases in cfg["stack"]["vocab"].items()}


def extract(text: str, cfg: dict) -> list[str]:
    """Technologies named in text, in order of first appearance."""
    found = []
    for tech, pats in _patterns(cfg).items():
        hits = [m.start() for p in pats for m in p.finditer(text or "")]
        if hits:
            found.append((min(hits), tech))
    return [t for _, t in sorted(found)]


def profile_stack(profile: dict, cfg: dict) -> dict[str, str]:
    """technology -> tier (production / projects / other) for the candidate."""
    out = {}
    skills = profile.get("skills") or {}
    for tier in TIER_ORDER:
        for skill in skills.get(tier) or []:
            for tech in extract(skill.replace("-", " "), cfg):
                out.setdefault(tech, tier)
    return out


@dataclass
class StackMatch:
    asked: list[str] = field(default_factory=list)      # the listing's stack, in its order
    mention: list[str] = field(default_factory=list)    # asked AND candidate has: lean on these
    gaps: list[str] = field(default_factory=list)       # asked, candidate lacks: never claim

    def summary(self) -> str:
        if not self.asked:
            return ""
        have = len(self.asked) - len(self.gaps)
        return f"Their stack: {', '.join(self.asked[:6])} · you match {have} of {len(self.asked)}"


def match(listing_text: str, profile: dict, cfg: dict) -> StackMatch:
    asked = [t for t in extract(listing_text, cfg) if t != "claude code"]
    mine = profile_stack(profile, cfg)
    have = [t for t in asked if t in mine]
    # production and project skills first, "other" skills last (can be named, never with stories)
    have.sort(key=lambda t: (TIER_ORDER.index(mine[t]), asked.index(t)))
    return StackMatch(asked=asked, mention=have[:cfg["stack"]["mention_max"]],
                      gaps=[t for t in asked if t not in mine])


def check(text: str, job: dict, profile: dict, cfg: dict, require_mention: bool) -> list[str]:
    """Problems with the technologies a draft names."""
    named = extract(text, cfg)
    mine = profile_stack(profile, cfg)
    problems = [f"names a technology not in your profile: {t}" for t in named if t not in mine]
    sm = job.get("stack")
    if sm:
        if require_mention and sm.mention and not set(named) & set(sm.mention):
            problems.append(f"doesn't mention any of their stack you have ({', '.join(sm.mention)})")
        unasked = [t for t in named if t not in sm.asked and t != "claude code"]
        # A listing that names under 2 technologies is generic: describing your broad stack is fine.
        if len(sm.asked) >= 2 and len(unasked) > cfg["stack"]["max_unasked"]:
            problems.append(f"names too much stack they didn't ask for: {', '.join(unasked)}")
    return problems
