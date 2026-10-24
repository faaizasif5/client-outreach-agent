"""Once-per-day guard (spec section 19a). Triggers fire often; this decides whether to run."""
import json
import os
import socket
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


def internet_ok(host: str = "hn.algolia.com", port: int = 443, timeout: float = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False
