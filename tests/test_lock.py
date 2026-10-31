from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from termctl import lock
from termctl.errors import TermctlError
from termctl.gateway import SystemClock

from .fakes import FakeClock

SOCKET = "/tmp/tmux-1000/default"
SRC = str(Path(__file__).resolve().parents[1] / "src")

# Holds the lock for argv[2] until killed or until stdin closes; prints "held" once it has it.
HOLDER = """
import os, sys
from termctl import lock
from termctl.gateway import SystemClock
env = {"XDG_STATE_HOME": sys.argv[1]}
with lock.server_lock(sys.argv[2], environ=env, clock=SystemClock()):
    print("held", flush=True)
    sys.stdin.read()
"""


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state")}


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    """A separate process holding the lock for SOCKET. Only this exact child is signaled."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / "state"), SOCKET],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC},
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == "held\n"
    yield proc
    if proc.poll() is None:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(10)


def test_lock_name_is_sha256_prefix_of_socket_path() -> None:
    digest = hashlib.sha256(SOCKET.encode()).hexdigest()[:16]
    assert lock.lock_name(SOCKET) == f"{digest}.lock"


def test_lock_name_hashes_unicode_as_utf8() -> None:
    path = "/tmp/sökét"
    assert lock.lock_name(path) == hashlib.sha256(path.encode()).hexdigest()[:16] + ".lock"


@pytest.mark.parametrize("bad", ["", "relative", "/tmp/\udcff"])
def test_lock_name_rejects_non_canonical_input(bad: str) -> None:
    with pytest.raises(ValueError):
        lock.lock_name(bad)


def test_lock_file_is_created_private_under_locks(environ: dict[str, str], tmp_path: Path) -> None:
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
        path = tmp_path / "state" / "termctl" / "locks" / lock.lock_name(SOCKET)
        assert stat.S_ISREG(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


def test_lock_is_reusable_after_release(environ: dict[str, str]) -> None:
    for _ in range(3):
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            assert lock.held()
        assert not lock.held()


def test_lock_is_released_when_the_body_raises(environ: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            raise RuntimeError("boom")
    assert not lock.held()
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=0):
        pass


def test_nested_acquisition_is_a_programming_error(environ: dict[str, str]) -> None:
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
        with pytest.raises(RuntimeError, match="already held"):
            with lock.server_lock("/other", environ=environ, clock=SystemClock()):
                pass
        assert lock.held()


def test_second_holder_times_out_after_10_seconds(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    clock = FakeClock()
    start = clock.monotonic()
    with pytest.raises(TermctlError) as info:
        with lock.server_lock(SOCKET, environ=environ, clock=clock, sleep=clock.advance):
            pytest.fail("acquired a held lock")
    assert info.value.code == "locked"
    assert info.value.exit_code == 12
    assert info.value.details == {"socket_path": SOCKET, "cause": "timeout"}
    assert clock.monotonic() - start == pytest.approx(10.0)
    assert not lock.held()


def test_default_timeout_is_10_seconds() -> None:
    assert lock.TIMEOUT_S == 10.0


def test_other_servers_are_not_blocked(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    with lock.server_lock("/tmp/other-socket", environ=environ, clock=SystemClock(), timeout=0):
        pass


def test_second_holder_waits_then_acquires_when_released(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 3:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(10)

    with lock.server_lock(SOCKET, environ=environ, clock=clock, sleep=sleep):
        assert lock.held()
    assert len(sleeps) == 3
    assert all(0 < s <= lock.POLL_S for s in sleeps)


def test_real_wait_with_a_real_clock(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    start = time.monotonic()
    with pytest.raises(TermctlError):
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=0.3):
            pass
    assert 0.3 <= time.monotonic() - start < 3


def test_lock_is_released_when_the_holder_dies(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    with pytest.raises(TermctlError):
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=0):
            pass
    os.kill(holder.pid, signal.SIGKILL)  # the exact child this test started
    holder.wait(10)
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=5):
        assert lock.held()


def test_stale_lock_file_left_by_a_dead_holder_is_reused(
    environ: dict[str, str], tmp_path: Path
) -> None:
    locks = tmp_path / "state" / "termctl" / "locks"
    locks.parent.parent.mkdir()
    locks.parent.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    (locks / lock.lock_name(SOCKET)).write_text("")
    (locks / lock.lock_name(SOCKET)).chmod(0o600)
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=0):
        pass


def test_symlinked_lock_file_is_refused(environ: dict[str, str], tmp_path: Path) -> None:
    locks = tmp_path / "state" / "termctl" / "locks"
    locks.parent.parent.mkdir()
    locks.parent.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (locks / lock.lock_name(SOCKET)).symlink_to(victim)
    with pytest.raises(TermctlError) as info:
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            pass
    assert info.value.code == "locked"
    assert info.value.details == {
        "socket_path": SOCKET,
        "cause": "unusable",
        "path": str(locks / lock.lock_name(SOCKET)),
        "problem": "is a symlink",
    }
    assert victim.read_text() == "keep"
    assert not lock.held()


def test_symlinked_locks_dir_is_refused(environ: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "termctl").mkdir(mode=0o700)
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    (tmp_path / "state" / "termctl" / "locks").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(TermctlError) as info:
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            pass
    assert info.value.code == "locked"
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_unusable_state_dir_exits_locked(environ: dict[str, str], tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "termctl").write_text("not a dir")
    with pytest.raises(TermctlError) as info:
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            pass
    assert info.value.code == "locked"
    path = str(tmp_path / "state" / "termctl")
    assert info.value.details == {
        "socket_path": SOCKET,
        "cause": "unusable",
        "path": path,
        "problem": "not a directory",
    }
    assert path in info.value.message


def test_flock_failure_exits_locked_unusable(
    environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr(lock.fcntl, "flock", no_locks)
    with pytest.raises(TermctlError) as info:
        with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
            pass
    assert info.value.code == "locked"
    assert info.value.details == {
        "socket_path": SOCKET,
        "cause": "unusable",
        "path": str(tmp_path / "state" / "termctl" / "locks" / lock.lock_name(SOCKET)),
        "problem": f"cannot lock: {os.strerror(errno.ENOLCK)}",
    }
    assert not lock.held()


def test_lock_file_deleted_before_flock_is_reopened(
    environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock taken on a file whose name was deleted would let a second process lock a new
    file of the same name; acquisition ends on the inode the name points to."""
    path = tmp_path / "state" / "termctl" / "locks" / lock.lock_name(SOCKET)
    real_flock = fcntl.flock
    calls: list[int] = []

    def flock_after_delete(fd: int, operation: int) -> None:
        calls.append(fd)
        if len(calls) == 1:
            path.unlink()
        real_flock(fd, operation)

    monkeypatch.setattr(lock.fcntl, "flock", flock_after_delete)
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock(), timeout=0):
        monkeypatch.setattr(lock.fcntl, "flock", real_flock)
        assert path.exists()
        other = os.open(path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    assert len(calls) == 2


def test_held_lock_fd_is_close_on_exec(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock descriptor never reaches a child (a hook or a tmux server) started while the
    lock is held."""
    real_flock = fcntl.flock
    held: list[int] = []

    def record(fd: int, operation: int) -> None:
        held.append(fd)
        real_flock(fd, operation)

    monkeypatch.setattr(lock.fcntl, "flock", record)
    with lock.server_lock(SOCKET, environ=environ, clock=SystemClock()):
        (fd,) = held
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        assert not os.get_inheritable(fd)
