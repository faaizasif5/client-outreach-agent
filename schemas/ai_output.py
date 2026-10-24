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
