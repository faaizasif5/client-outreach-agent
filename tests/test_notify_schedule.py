import json
from datetime import timedelta

import pytest

from outputs import notify
from pipeline import schedule
from tests.conftest import NOW


@pytest.mark.parametrize("counts,message,priority", [
    ({"drafts_created": 6, "kits_created": 2, "uncertain": 3},
     "6 drafts ready + 2 new portal jobs in the tracker. 3 jobs to review.", "default"),
    ({}, "No new matches today.", "low"),
    ({"replies": 1}, "New reply on 1 outreach email.", "high"),
    ({"failed_attempts": 3, "error_type": "ClaudeError"}, "Job agent failed today: ClaudeError.", "high"),
])
def test_messages(counts, message, priority):
    note = notify.compose(counts)
    assert (note["message"], note["priority"]) == (message, priority)


def test_body_never_contains_job_details():
    counts = {"drafts_created": 2, "kits_created": 1, "uncertain": 0, "company": "Acme", "title": "Backend"}
    note = notify.compose(counts)
    assert "Acme" not in json.dumps(note) and "Backend" not in json.dumps(note)


class Resp:
    def __init__(self, ok=True):
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("500")


def test_send_headers_and_failure_never_raises():
    calls = []

    def post(url, data, headers, timeout):
        calls.append((url, data, headers))
        return Resp()
    assert notify.send({"drafts_created": 1, "kits_created": 1}, topic="t123", enabled=True,
                       server="https://ntfy.sh", sheet_url="https://docs.google.com/x", post=post)
    url, data, headers = calls[0]
    assert url == "https://ntfy.sh/t123" and headers["Click"] == notify.GMAIL_DRAFTS
    assert headers["Actions"] == "view, Open tracker, https://docs.google.com/x"

    def down(*a, **k):
        raise ConnectionError("ntfy down")
    assert notify.send({}, topic="t", enabled=True, server="https://ntfy.sh", post=down) is False
    assert notify.send({}, topic="t", enabled=False, server="https://ntfy.sh", post=post) is False


def _run(conn, at, status):
    conn.execute("INSERT INTO runs (run_id, started_at, status) VALUES (?,?,?)",
                 (at.isoformat(), at.strftime("%Y-%m-%dT%H:%M:%SZ"), status))


def test_second_trigger_same_day_exits(conn):
    assert schedule.guard_reason(conn, NOW, 3) is None
    _run(conn, NOW, "success")
    assert schedule.guard_reason(conn, NOW + timedelta(hours=3), 3) == "already ran successfully today"
    # Next Karachi day runs again (NOW is 11:00 PKT; +14h is next day)
    assert schedule.guard_reason(conn, NOW + timedelta(hours=14), 3) is None


def test_failed_runs_retry_until_max_attempts(conn):
    for i in range(2):
        _run(conn, NOW + timedelta(minutes=i), "failed")
    assert schedule.guard_reason(conn, NOW + timedelta(hours=1), 3) is None
    _run(conn, NOW + timedelta(minutes=5), "failed")
    assert schedule.guard_reason(conn, NOW + timedelta(hours=1), 3) == "3 failed attempts today"


def test_lock(tmp_path):
    lock = tmp_path / "run.lock"
    with schedule.run_lock(lock, 60) as held:
        assert held
        with schedule.run_lock(lock, 60) as second:
            assert not second
    assert not lock.exists()


def test_stale_lock_cleared(tmp_path):
    import os
    import time
    lock = tmp_path / "run.lock"
    lock.write_text("{}")
    old = time.time() - 2 * 3600
    os.utime(lock, (old, old))
    with schedule.run_lock(lock, 60) as held:
        assert held


class FakeSchtasks:
    """Stands in for schtasks.exe: tracks the task's state, never touches the real scheduler."""
    def __init__(self, installed=True):
        self.status, self.calls = ("Ready" if installed else None), []

    def __call__(self, *args):
        import subprocess
        self.calls.append(args)
        if self.status is None:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified.")
        if args[0] == "/Change":
            self.status = "Ready" if "/ENABLE" in args else "Disabled"
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        return subprocess.CompletedProcess(args, 0, f'"\\JobOutreachAgent","N/A","{self.status}"\n', "")


def test_scheduler_off_and_on(monkeypatch, capsys):
    import main
    fake = FakeSchtasks()
    monkeypatch.setattr(schedule, "_schtasks", fake)
    assert main.main(["scheduler", "off"]) == 0
    assert "JobOutreachAgent: Disabled" in capsys.readouterr().out
    assert ("/Change", "/TN", "JobOutreachAgent", "/DISABLE") in fake.calls
    assert main.main(["scheduler"]) == 0                      # status is the default
    assert "Disabled" in capsys.readouterr().out
    assert main.main(["scheduler", "on"]) == 0
    assert "JobOutreachAgent: Ready" in capsys.readouterr().out


def test_scheduler_not_installed_and_bad_action(monkeypatch, capsys):
    import main
    monkeypatch.setattr(schedule, "_schtasks", FakeSchtasks(installed=False))
    assert main.main(["scheduler", "status"]) == 0
    assert "not installed" in capsys.readouterr().out
    assert main.main(["scheduler", "off"]) == 1
    assert "install_task.ps1" in capsys.readouterr().out
    assert main.main(["scheduler", "pause"]) == 2
