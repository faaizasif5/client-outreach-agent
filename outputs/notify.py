"""ntfy push after each run (spec section 14). Public topics can be read by anyone who guesses
the name, so messages carry counts only: no company names, titles, emails or job links."""
import logging

import requests

log = logging.getLogger(__name__)
GMAIL_DRAFTS = "https://mail.google.com/mail/u/0/#drafts"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def compose(counts: dict) -> dict:
    """Title / message / priority / tags from run counts only."""
    if counts.get("failed_attempts", 0) >= 3:
        return {"title": "Job agent", "priority": "high", "tags": "warning",
                "message": f"Job agent failed today: {counts.get('error_type') or 'error'}."}
    drafts, kits = counts.get("drafts_created", 0), counts.get("kits_created", 0)
    review, replies = counts.get("uncertain", 0), counts.get("replies", 0)
    parts = []
    if replies:
        parts.append(f"New reply on {_plural(replies, 'outreach email')}.")
    if drafts or kits:
        parts.append(f"{_plural(drafts, 'draft')} ready + {_plural(kits, 'new portal job')} in the tracker. "
                     f"{_plural(review, 'job')} to review.")
    if counts.get("yc_manual_fix"):
        parts.append("YC scraper needs a manual fix.")
    if not parts:
        return {"title": "Job agent", "priority": "low", "tags": "zzz", "message": "No new matches today."}
    return {"title": "Job agent", "priority": "high" if replies else "default",
            "tags": "envelope" if drafts else "clipboard", "message": " ".join(parts)}


def send(counts: dict, *, topic: str | None, enabled: bool, server: str, sheet_url: str | None = None,
         timeout: int = 10, post=requests.post) -> bool:
    """Never raises: a notification failure is logged and the pipeline continues."""
    if not enabled or not topic:
        return False
    note = compose(counts)
    headers = {"Title": note["title"], "Priority": note["priority"], "Tags": note["tags"], "Click": GMAIL_DRAFTS}
    if sheet_url and counts.get("kits_created"):
        headers["Actions"] = f"view, Open tracker, {sheet_url}"
    try:
        resp = post(f"{server.rstrip('/')}/{topic}", data=note["message"].encode("utf-8"), headers=headers,
                    timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy failed: %s", e)
        return False
