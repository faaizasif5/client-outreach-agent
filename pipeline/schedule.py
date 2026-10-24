"""Once-per-day guard (spec section 19a). Triggers fire often; this decides whether to run."""
import csv
import io
import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import repo

PKT = timezone(timedelta(hours=5))   # Asia/Karachi, no DST


def local_date(dt: datetime) -> str:
    return dt.astimezone(PKT).date().isoformat()


def runs_today(conn, now: datetime) -> list:
    today = local_date(now)
    return [r for r in conn.execute("SELECT run_id, started_at, status, errors FROM runs").fetchall()
            if r["started_at"] and local_date(repo.parse_iso(r["started_at"])) == today]


def guard_reason(conn, now: datetime, max_attempts: int) -> str | None:
    """Why this trigger should exit without running, or None to run."""
    today = runs_today(conn, now)
    if any(r["status"] == "success" for r in today):
        return "already ran successfully today"
    if sum(1 for r in today if r["status"] == "failed") >= max_attempts:
        return f"{max_attempts} failed attempts today"
    return None


def failed_attempts_today(conn, now: datetime) -> int:
    return sum(1 for r in runs_today(conn, now) if r["status"] == "failed")


@contextmanager
def run_lock(path: Path, stale_minutes: int):
    """Yields True if we hold the lock, False if another run does. Stale locks are cleared."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > stale_minutes * 60:
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
        os.close(fd)
        yield True
    finally:
        path.unlink(missing_ok=True)


TASK_NAME = "ClientOutreachAgent"   # registered by scripts/install_task.ps1


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def task_status() -> str:
    """'Ready', 'Running', 'Disabled', ... as Task Scheduler reports it, or 'not installed'."""
    out = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH")
    if out.returncode != 0:
        return "not installed"
    row = next(csv.reader(io.StringIO(out.stdout.strip().splitlines()[-1])))
    return row[-1]


def set_task_enabled(enabled: bool) -> str:
    """Turns the daily task on or off without unregistering it. Returns the new status."""
    if task_status() == "not installed":
        raise RuntimeError(f"{TASK_NAME} is not registered; run scripts\\install_task.ps1 first")
    out = _schtasks("/Change", "/TN", TASK_NAME, "/ENABLE" if enabled else "/DISABLE")
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip())
    return task_status()


def internet_ok(host: str = "hn.algolia.com", port: int = 443, timeout: float = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False
