"""Automatic YC selector repair (spec 7a): only when the scraper is broken, at most once a week.
Claude (tools off) sees the old rules and a stripped snapshot and returns new rules as JSON only,
never code. New rules are accepted only if every health check passes on the snapshot and at least
5 listings extract cleanly. Old rules are backed up first; a rejected repair changes nothing."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from db import repo
from fetchers import yc_health, yc_scraper
from pipeline.claude_cli import ClaudeError, run_claude

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "prompts" / "yc_repair.md"
REQUIRED = {"title", "company", "url"}
SAFE_TEXT = r"^[^<>`{}\\]*$"


class JsonRules(BaseModel):
    container: str = Field(max_length=200, pattern=SAFE_TEXT)
    attribute: str = Field(max_length=60, pattern=r"^[\w-]+$")
    listings_path: str = Field(max_length=200, pattern=r"^[\w.]+$")
    fields: dict[str, str]

    @field_validator("fields")
    @classmethod
    def required(cls, v):
        if not REQUIRED <= set(v):
            raise ValueError(f"fields must include {sorted(REQUIRED)}")
        if any(len(x) > 100 or not x.replace("_", "").replace(".", "").isalnum() for x in v.values()):
            raise ValueError("field keys must be plain names")
        return v


class CssField(BaseModel):
    selector: str = Field(max_length=200, pattern=SAFE_TEXT)
    attr: str = Field(max_length=40, pattern=r"^[\w-]+$")


class CssRules(BaseModel):
    container: str = Field(max_length=200, pattern=SAFE_TEXT)
    card: str = Field(max_length=200, pattern=SAFE_TEXT)
    fields: dict[str, CssField]

    @field_validator("fields")
    @classmethod
    def required(cls, v):
        if not REQUIRED <= set(v):
            raise ValueError(f"fields must include {sorted(REQUIRED)}")
        return v


class SelectorRepair(BaseModel):
    mode: Literal["json", "css"]
    json_rules: JsonRules | None = Field(default=None, alias="json")
    css: CssRules | None = None


def recent_auto_attempt(conn, now: datetime, page: str | None = None) -> bool:
    row = conn.execute("SELECT MAX(attempted_at) FROM yc_repairs WHERE kind='auto' AND (? IS NULL OR page = ?)",
                       (page, page)).fetchone()[0]
    return bool(row) and repo.parse_iso(row) > now - timedelta(days=7)


def candidate_rules(old_page: dict, fix: SelectorRepair) -> dict:
    """New rules for one page; everything Claude did not return (url, detail, ...) is kept."""
    new = dict(old_page)
    new["mode"] = fix.mode
    if fix.json_rules:
        new["json"] = {**fix.json_rules.model_dump()}
    if fix.css:
        new["css"] = fix.css.model_dump()
    return new


def test_rules(snapshot: str, rules: dict, recent: list[int]) -> tuple[bool, dict]:
    result = yc_health.check(200, snapshot, rules, recent)
    clean = [l for l in result["listings"] if l.get("title") and l.get("company") and l.get("url")]
    ok = result["status"] == "healthy" and len(clean) >= 5
    return ok, {"checks": result["checks"], "clean_listings": len(clean)}


def attempt(conn, snapshot: str, selectors_path: Path, now: datetime, *, page: str, model: str = "sonnet",
            claude=run_claude) -> dict:
    """Repair one page's rules. Returns {'accepted': bool, 'reason': str, ...} and records the attempt."""
    if recent_auto_attempt(conn, now, page):
        return {"accepted": False, "reason": "already tried this week"}
    doc = yc_scraper.load_selectors(selectors_path)
    old = yc_scraper.page_rules(doc, page)
    shown = {k: v for k, v in old.items() if k in ("results_url", "mode", "json", "css", "url_template")}
    prompt = PROMPT.read_text(encoding="utf-8").format(
        selectors=yaml.safe_dump(shown, sort_keys=False), snapshot=snapshot[:40000])
    details: dict = {}
    try:
        fix, usage = claude(prompt, model, SelectorRepair, retries=1, timeout=600)
        details["usage"] = usage
        rules = candidate_rules(old, fix)
        accepted, details["test"] = test_rules(snapshot, rules, yc_health.recent_counts(conn))
    except (ClaudeError, ValidationError) as e:   # invalid selectors are a rejected repair
        accepted, details["error"] = False, str(e)[:300]
    if accepted:
        backup = selectors_path.with_name(f"yc_selectors.{now:%Y-%m-%d}.bak.yaml")
        backup.write_text(selectors_path.read_text(encoding="utf-8"), encoding="utf-8")
        for i, p in enumerate(doc["pages"]):
            if p["name"] == page:
                doc["pages"][i] = {k: v for k, v in rules.items() if k != "allowed_domains"}
        doc["version"] = int(doc.get("version", 1)) + 1
        selectors_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        details["backup"] = str(backup)
    conn.execute("INSERT INTO yc_repairs (attempted_at, kind, accepted, details, page) VALUES (?,?,?,?,?)",
                 (repo.iso(now), "auto", int(accepted), json.dumps(details, default=str), page))
    return {"accepted": accepted, "reason": "accepted" if accepted else "rejected", **details}
