"""The real Claude CLI is never called: claude_cli._run is replaced in every test."""
import json
import subprocess

import pytest

from pipeline import claude_cli
from schemas.ai_output import ScoreBatch

GOOD = {"results": [{"job_key": "k1", "resume": "fullstack", "fit_score": 82,
                     "evidence": ["Listing: 'Django'"], "reason": "Backend heavy",
                     "confidence": "high", "skip": False, "skip_reasons": []}]}


def envelope(result_text, **extra):
    return json.dumps({"type": "result", "result": result_text, "is_error": False,
                       "usage": {"input_tokens": 1200, "output_tokens": 300},
                       "duration_ms": 4321, "total_cost_usd": 0.01, "num_turns": 1, **extra})


class FakeRun:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


@pytest.fixture
def fake(monkeypatch):
    def install(outputs):
        f = FakeRun(outputs)
        monkeypatch.setattr(claude_cli, "_run", f)
        return f
    return install


def test_flags_disable_tools_and_use_json(fake):
    f = fake([envelope(json.dumps(GOOD))])
    parsed, usage = claude_cli.run_claude("prompt", "haiku", ScoreBatch, sleep=lambda s: None)
    cmd, kw = f.calls[0]
    assert cmd[1:5] == ["-p", "--model", "haiku", "--output-format"] and "json" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--bare" not in cmd and "--strict-mcp-config" in cmd
    assert kw["input"] == "prompt" and kw["cwd"] == claude_cli.SANDBOX
    assert "CLAUDECODE" not in kw["env"]
    assert parsed.results[0].fit_score == 82
    assert usage["calls"][0]["usage"] == {"input_tokens": 1200, "output_tokens": 300}
    assert usage["calls"][0]["duration_ms"] == 4321


def test_code_fences_and_prose_are_stripped(fake):
    fake([envelope("Sure! Here you go:\n```json\n" + json.dumps(GOOD) + "\n```")])
    parsed, _ = claude_cli.run_claude("p", "haiku", ScoreBatch, sleep=lambda s: None)
    assert parsed.results[0].job_key == "k1"


def test_malformed_json_retries_then_succeeds(fake):
    f = fake([envelope("not json at all"), envelope(json.dumps(GOOD))])
    parsed, usage = claude_cli.run_claude("p", "haiku", ScoreBatch, sleep=lambda s: None)
    assert usage["attempts"] == 2
    assert "Return ONLY valid JSON" in f.calls[1][1]["input"]


def test_malformed_every_time_fails_cleanly(fake):
    bad = dict(GOOD, results=[dict(GOOD["results"][0], fit_score=120)])
    fake([envelope("{oops"), envelope(json.dumps(bad)), subprocess.TimeoutExpired("claude", 1)])
    with pytest.raises(claude_cli.ClaudeError) as e:
        claude_cli.run_claude("p", "haiku", ScoreBatch, retries=2, sleep=lambda s: None)
    assert e.value.usage["attempts"] == 3


def test_nonzero_exit_is_an_error(fake, monkeypatch):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not logged in")
    monkeypatch.setattr(claude_cli, "_run", run)
    with pytest.raises(claude_cli.ClaudeError, match="Not logged in"):
        claude_cli.run_claude("p", "haiku", ScoreBatch, retries=0, sleep=lambda s: None)


def test_effort_flag_only_when_set(fake):
    f = fake([envelope(json.dumps(GOOD)), envelope(json.dumps(GOOD))])
    claude_cli.run_claude("p", "sonnet", ScoreBatch, sleep=lambda s: None, effort="medium")
    claude_cli.run_claude("p", "haiku", ScoreBatch, sleep=lambda s: None)
    with_effort, without = f.calls[0][0], f.calls[1][0]
    assert with_effort[with_effort.index("--effort") + 1] == "medium"
    assert "--effort" not in without
