"""Tests for src.run_lock: one benchmark or order batch at a time, across threads and processes."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import types

import pytest

from src import run_lock
from src.run_lock import RunBusyError, current_run, exclusive_run, lock_path_for
from src.settings import PROJECT_ROOT


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "api_run.lock"


def try_from_thread(path, **kw) -> Exception | None:
    """Try to take the lock from another thread; return what it raised."""
    box: list[Exception | None] = []

    def work():
        try:
            with exclusive_run(path, owner="other", kind="benchmark", **kw):
                box.append(None)
        except Exception as exc:
            box.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    return box[0]


def test_lock_path_sits_next_to_the_database(tmp_path):
    assert lock_path_for(tmp_path / "benchmark.db") == tmp_path / "api_run.lock"
    assert lock_path_for(str(tmp_path / "benchmark.db")) == tmp_path / "api_run.lock"


def test_second_acquire_from_another_thread_names_the_owner(path):
    with exclusive_run(path, owner="alice", kind="order test"):
        exc = try_from_thread(path)
    assert isinstance(exc, RunBusyError) and "alice" in str(exc) and "order test" in str(exc)


def test_second_acquire_from_the_same_thread_is_refused(path):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        with pytest.raises(RunBusyError, match="alice"):
            with exclusive_run(path, owner="bob", kind="benchmark"):
                pytest.fail("the lock must not be re-entrant")


def test_refused_acquire_does_not_disturb_the_holder(path):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        assert isinstance(try_from_thread(path), RunBusyError)
        assert current_run(path)["owner"] == "alice"
        assert isinstance(try_from_thread(path), RunBusyError)      # still held


def test_released_after_the_block(path):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        pass
    assert try_from_thread(path) is None
    with exclusive_run(path, owner="bob", kind="benchmark"):
        pass


def test_released_after_an_exception(path):
    with pytest.raises(ValueError):
        with exclusive_run(path, owner="alice", kind="benchmark"):
            raise ValueError("boom")
    assert current_run(path) is None
    assert try_from_thread(path) is None


def test_current_run_shows_info_then_none(path):
    assert current_run(path) is None                            # no lock file yet
    with exclusive_run(path, owner="alice", kind="order test"):
        info = current_run(path)
        assert info["owner"] == "alice" and info["kind"] == "order test" and info["started_at"]
    assert current_run(path) is None
    assert not run_lock._info_path(path).exists()


def test_current_run_does_not_take_the_lock_away(path):
    current_run(path)
    with exclusive_run(path, owner="alice", kind="benchmark"):
        current_run(path)
        current_run(path)
        assert isinstance(try_from_thread(path), RunBusyError)


def test_stale_info_from_a_dead_process_is_ignored(path):
    path.parent.mkdir(parents=True)
    path.touch()
    run_lock._info_path(path).write_text('{"owner": "ghost", "kind": "benchmark"}', encoding="utf-8")
    assert current_run(path) is None
    with exclusive_run(path, owner="alice", kind="benchmark"):
        assert current_run(path)["owner"] == "alice"


def test_corrupt_info_still_reports_busy(path):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        run_lock._info_path(path).write_text("{oops", encoding="utf-8")
        assert current_run(path) == {"owner": "", "kind": "run"}
        with pytest.raises(RunBusyError, match="another user"):
            with exclusive_run(path, owner="bob", kind="benchmark"):
                pass


def test_a_status_probe_holding_the_lock_for_an_instant_does_not_fail_a_run(path, monkeypatch):
    real = run_lock._try_lock
    tries = []

    def collides_once(fd):
        tries.append(fd)
        return real(fd) if len(tries) > 1 else False

    monkeypatch.setattr(run_lock, "_try_lock", collides_once)
    monkeypatch.setattr(run_lock.time, "sleep", lambda s: None)
    with exclusive_run(path, owner="alice", kind="benchmark"):
        assert run_lock._read_info(path)["owner"] == "alice"
    assert len(tries) == 2


def test_lock_holds_across_processes_and_a_killed_process_releases_it(path):
    script = ("import sys\nfrom pathlib import Path\nfrom src.run_lock import exclusive_run\n"
              "with exclusive_run(Path(sys.argv[1]), owner='other-process', kind='benchmark'):\n"
              "    print('READY', flush=True)\n    sys.stdin.read()\n")
    proc = subprocess.Popen([sys.executable, "-c", script, str(path)], cwd=PROJECT_ROOT, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == "READY"
        with pytest.raises(RunBusyError, match="other-process"):
            with exclusive_run(path, owner="alice", kind="benchmark"):
                pass
        assert current_run(path)["owner"] == "other-process"
    finally:
        proc.kill()
        proc.communicate()
    assert current_run(path) is None                            # the stale .info file is ignored too
    with exclusive_run(path, owner="alice", kind="benchmark"):
        assert current_run(path)["owner"] == "alice"


def test_unlink_failure_on_release_does_not_keep_the_lock(path, monkeypatch):
    """Windows refuses to delete a file another thread is reading; that must not mask the release."""
    real_unlink = type(path).unlink

    def flaky(self, missing_ok=False):
        if self.name.endswith(".info"):
            raise PermissionError("in use")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(path), "unlink", flaky)
    with exclusive_run(path, owner="alice", kind="benchmark"):
        pass
    monkeypatch.undo()
    assert current_run(path) is None


# ---------------------------------------------------------------------------- platform branches
class FakeFlock(types.ModuleType):
    """fcntl stand-in with flock's semantics: the lock belongs to the open file, so separate fds conflict."""

    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self):
        super().__init__("fcntl")
        self.holder: int | None = None
        self.calls: list[tuple[int, int]] = []

    def flock(self, fd, op):
        self.calls.append((fd, op))
        if op == self.LOCK_UN:
            if self.holder == fd:
                self.holder = None
        elif self.holder not in (None, fd):
            raise BlockingIOError("would block")
        else:
            self.holder = fd


@pytest.fixture
def posix(monkeypatch):
    fake = FakeFlock()
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(run_lock.sys, "platform", "linux")
    monkeypatch.setattr(run_lock.time, "sleep", lambda s: None)
    return fake


def test_posix_uses_an_exclusive_nonblocking_flock_and_unlocks(path, posix):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        fd = posix.holder
        assert fd is not None
        assert posix.calls == [(fd, posix.LOCK_EX | posix.LOCK_NB)]
    assert posix.holder is None and posix.calls[-1] == (fd, posix.LOCK_UN)


def test_posix_second_descriptor_conflicts_within_one_process(path, posix):
    with exclusive_run(path, owner="alice", kind="benchmark"):
        with pytest.raises(RunBusyError, match="alice"):
            with exclusive_run(path, owner="bob", kind="benchmark"):
                pass
        assert current_run(path)["owner"] == "alice"
    assert current_run(path) is None and posix.holder is None


def test_each_acquire_opens_its_own_descriptor(path, monkeypatch):
    """Both flock (open file description) and msvcrt (handle) only conflict across separate opens."""
    opened = []
    real = os.open
    monkeypatch.setattr(run_lock.os, "open", lambda *a: opened.append(a[0]) or real(*a))
    with exclusive_run(path, owner="alice", kind="benchmark"):
        with pytest.raises(RunBusyError):
            with exclusive_run(path, owner="bob", kind="benchmark"):
                pass
    assert len(opened) == 2


def test_lock_and_unlock_really_work_on_this_platform(path):
    path.parent.mkdir(parents=True)
    a = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    b = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        assert run_lock._try_lock(a)
        assert not run_lock._try_lock(b)
        run_lock._unlock(a)
        assert run_lock._try_lock(b)
        run_lock._unlock(b)
    finally:
        os.close(a)
        os.close(b)


def test_closing_the_descriptor_releases_the_lock(path):
    """A dead process's descriptors are closed by the OS, so a crash can't leave the lock stuck."""
    path.parent.mkdir(parents=True)
    a = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    assert run_lock._try_lock(a)
    os.close(a)
    assert current_run(path) is None
