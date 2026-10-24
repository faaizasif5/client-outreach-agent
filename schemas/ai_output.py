"""Pydantic models for Claude output (spec section 10)."""
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ResumeKey(str, Enum):
    fullstack = "fullstack"
    fde = "fde"
    react_next = "react_next"
    python_django = "python_django"
    dotnet = "dotnet"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class SkipReason(str, Enum):
    stack_mismatch = "stack_mismatch"
    too_senior = "too_senior"
    too_junior = "too_junior"
    domain_mismatch = "domain_mismatch"
    hidden_location_restriction = "hidden_location_restriction"
    insufficient_info = "insufficient_info"


class JobScore(BaseModel):
    job_key: str
    resume: ResumeKey
    fit_score: int = Field(ge=0, le=100)
    evidence: list[str] = Field(min_length=1, max_length=3)   # quotes/facts from listing + profile
    reason: str = Field(max_length=200)                       # why this resume
    confidence: Confidence
    skip: bool
    skip_reasons: list[SkipReason] = []


class ScoreBatch(BaseModel):
    results: list[JobScore]

    @field_validator("results")
    @classmethod
    def no_duplicate_jobs(cls, v):
        keys = [r.job_key for r in v]
        if len(keys) != len(set(keys)):   # spec uses assert; ValueError survives `python -O`
            raise ValueError("duplicate job_key")
        return v


# ---------------------------------------------------------------- Claude call #2 (spec section 11)

class OutreachType(str, Enum):
    direct_email = "direct_email"
    application_brief = "application_brief"


class FormAnswer(BaseModel):
    question: str = Field(max_length=300)
    answer: str = Field(max_length=1500)


class Kit(BaseModel):
    cover_note: str = Field(min_length=1, max_length=900)      # 3-5 lines
    form_answers: list[FormAnswer] = Field(default_factory=list, max_length=8)


class NetworkNote(BaseModel):
    connection_note: str | None = Field(default=None, max_length=200)   # LinkedIn limit
    after_accept_message: str = Field(max_length=600)


class Draft(BaseModel):
    job_key: str
    outreach_type: OutreachType
    subject: str | None = Field(default=None, max_length=120)
    body: str = Field(default="", max_length=2000)
    kit: Kit | None = None
    network: NetworkNote | None = None


class DraftBatch(BaseModel):
    results: list[Draft]

    @field_validator("results")
    @classmethod
    def no_duplicate_jobs(cls, v):
        keys = [r.job_key for r in v]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate job_key")
        return v
