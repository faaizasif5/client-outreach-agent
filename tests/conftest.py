import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from db import repo

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def profile():
    return yaml.safe_load((ROOT / "candidate" / "profile.example.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    c = repo.connect(":memory:")
    yield c
    c.close()


def make_job(conn, job_key="src:hn:1", company="Acme", company_norm="acme",
             company_domain=None, title="Backend Engineer", **extra):
    row = {
        "job_key": job_key, "company": company, "company_norm": company_norm,
        "company_domain": company_domain, "title": title, "title_norm": title.lower(),
        "canonical_url": extra.pop("canonical_url", f"https://example.test/{job_key}"),
        "sources": json.dumps([{"source": "hn", "external_id": job_key, "url": ""}]),
        "first_seen_at": repo.iso(NOW), "last_seen_at": repo.iso(NOW),
    }
    row.update(extra)
    repo.insert_job(conn, row)
    return row


@pytest.fixture(autouse=True)
def isolated_resume_templates(tmp_path, monkeypatch):
    """Never read the real candidate/latex templates or bullet bank in tests."""
    from pipeline import resume_builder
    monkeypatch.setattr(resume_builder, "LATEX_DIR", tmp_path / "no_latex")
    monkeypatch.setattr(resume_builder, "BULLETS", tmp_path / "no_bullets.yaml")


@pytest.fixture(autouse=True)
def never_call_real_claude(monkeypatch):
    """Any test that reaches the real `claude` subprocess fails (tests that check the wrapper
    replace claude_cli._run themselves)."""
    from pipeline import claude_cli

    def refuse(*args, **kwargs):
        raise AssertionError("tests must never run the real Claude CLI")
    monkeypatch.setattr(claude_cli, "_run", refuse)
