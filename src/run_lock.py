"""Server-wide lock: one benchmark or order batch at a time.

Rate limits apply per TradeSmart account, while the app's limiters apply per run. Allowing one run at a time
is what keeps several users from adding up to more than the API allows. It is an OS file lock, so it also holds
across processes and is released automatically if the process dies.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

LOCK_FILE = "api_run.lock"
LOCK_ATTEMPTS = 3
LOCK_RETRY_S = 0.05


class RunBusyError(RuntimeError):
    """Another benchmark or order batch is running. Nothing has been sent."""


def lock_path_for(db_path: Path | str) -> Path:
    return Path(db_path).parent / LOCK_FILE


def _info_path(path: Path) -> Path:
    return path.with_name(path.name + ".info")


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _try_lock_patiently(fd: int) -> bool:
    """``_try_lock`` with a short retry: :func:`current_run` probes by taking the lock for an instant, and a status
    check on someone's screen must not make a run fail as busy."""
    for attempt in range(LOCK_ATTEMPTS):
        if attempt:
            time.sleep(LOCK_RETRY_S)
        if _try_lock(fd):
            return True
    return False


def _read_info(path: Path) -> dict:
    try:
        data = json.loads(_info_path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(info: dict) -> str:
    who = info.get("owner") or "another user"
    what = info.get("kind") or "run"
    return f"A {what} started by {who} is running. Only one can run at a time; try again when it finishes."


@contextmanager
def exclusive_run(path: Path, *, owner: str, kind: str) -> Iterator[None]:
    """Hold the lock for the block. Raises :class:`RunBusyError` at once if it is already held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        if not _try_lock_patiently(fd):
            raise RunBusyError(describe(_read_info(path)))
        try:
            _info_path(path).write_text(json.dumps(
                {"owner": owner, "kind": kind, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                encoding="utf-8")
            yield
        finally:
            try:
                _info_path(path).unlink(missing_ok=True)
            except OSError:  # Windows: a status check is reading it. Stale info is ignored once the lock is free.
                pass
            _unlock(fd)
    finally:
        os.close(fd)


def current_run(path: Path) -> dict | None:
    """Details of the run holding the lock, or None when free. For showing status in the UI."""
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        if _try_lock_patiently(fd):
            _unlock(fd)
            return None
        return _read_info(path) or {"owner": "", "kind": "run"}
    finally:
        os.close(fd)
