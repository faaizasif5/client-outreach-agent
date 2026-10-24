"""Stack matching: the listing's stack vs the profile, and what drafts may name."""
import pytest

from pipeline import stack


@pytest.mark.parametrize("text,expected", [
    ("We use Python, Postgres and TypeScript", ["python", "postgresql", "typescript"]),
    ("Next.js frontend with a FastAPI backend", ["next.js", "fastapi"]),          # not "javascript"
    ("Build LLM agents with OpenAI", ["llm apis"]),
    ("We ship AI agents on GPT", ["ai agents", "llm apis"]),
    ("I write Go services", ["go"]),
    ("Go, Rust and Kubernetes", ["go", "rust", "kubernetes"]),
    ("Let's go build something great", []),                                       # English "go"
    ("React Native mobile app", ["react native"]),
    ("Django REST Framework APIs and RESTful APIs", ["django rest framework", "rest apis"]),
    ("agentic coding with Claude Code", ["claude code"]),
])
def test_extract(cfg, text, expected):
    assert stack.extract(text, cfg) == expected


def test_profile_stack_tiers(cfg, profile):
    mine = stack.profile_stack(profile, cfg)
    assert mine["python"] == "production" and mine["fastapi"] == "projects" and mine["aws"] == "other"
    assert mine["claude code"] == "production"


def test_match_mentions_what_you_have_and_flags_gaps(cfg, profile):
    m = stack.match("Python, Go and AWS; Postgres; React front end", profile, cfg)
    assert m.asked == ["python", "go", "aws", "postgresql", "react"]
    assert m.mention == ["python", "postgresql", "react", "aws"]                  # production first, "other" last
    assert m.gaps == ["go"]
    assert m.summary() == "Their stack: python, go, aws, postgresql, react · you match 4 of 5"


def test_check_rejects_unowned_and_unmentioned(cfg, profile):
    job = {"stack": stack.match("Python, SQL, TypeScript", profile, cfg)}
    assert any("not in your profile: go" in p
               for p in stack.check("I write Go services", job, profile, cfg, require_mention=False))
    assert any("doesn't mention" in p
               for p in stack.check("I build web apps.", job, profile, cfg, require_mention=True))
    assert stack.check("I work in Python and TypeScript, with Claude Code.", job, profile, cfg, True) == []
    too_much = "Python plus React, Vue, Django and Redis"
    assert any("didn't ask for" in p for p in stack.check(too_much, job, profile, cfg, True))


def test_drafter_rejects_skill_claims_not_in_profile(cfg, profile):
    from pipeline import drafter
    from schemas.ai_output import Draft
    from tests.test_drafter import GOOD_BODY, job
    d = Draft(job_key="k1", outreach_type="direct_email", subject="s",
              body=GOOD_BODY.replace("Open to a short call", "I also write Rust. Open to a short call"))
    assert any("not in your profile: rust" in p for p in drafter.validate_draft(d, job(), profile, cfg))
