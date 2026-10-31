"""Helpers for tests that run real processes: they poll instead of sleeping fixed durations,
and count a zombie (killed, not yet reaped by its parent or init) as gone."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` seconds pass; whether it held."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def read_when_ready(path: Path, timeout: float = 5.0) -> str:
    """The text of ``path`` once it exists (written by a hook to ``path.tmp`` and renamed)."""
    assert wait_until(path.exists, timeout), f"{path} never appeared"
    return path.read_text()


def _ps(*args: str) -> str:
    return subprocess.run(["ps", *args], capture_output=True, text=True, check=False).stdout


def is_gone(pid: int) -> bool:
    """Whether ``pid`` no longer runs: missing, or a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        if Path("/proc/self/stat").exists():
            return True  # Linux, and the process is gone
        stat = None
    except OSError:
        stat = None
    if stat is not None:
        return stat.rsplit(")", 1)[1].split()[0] == "Z"
    state = _ps("-o", "stat=", "-p", str(pid)).strip()  # macOS: no /proc
    return not state or state.startswith("Z")


def wait_gone(*pids: int, timeout: float = 5.0) -> bool:
    return wait_until(lambda: all(is_gone(pid) for pid in pids), timeout)


def group_members(pgid: int) -> list[int]:
    """The processes of group ``pgid`` that still run (zombies excluded)."""
    members: list[int] = []
    for line in _ps("-A", "-o", "pid=,pgid=,stat=").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == str(pgid) and not fields[2].startswith("Z"):
            members.append(int(fields[0]))
    return members


def group_is_gone(pgid: int, timeout: float = 5.0) -> bool:
    return wait_until(lambda: not group_members(pgid), timeout)


def reap(pid: int, timeout: float = 5.0) -> bool:
    """Wait for this process's child ``pid`` to exit and reap it; whether it did in time.
    A child reaped meanwhile by ``subprocess``'s own cleanup has exited too."""

    def exited() -> bool:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return done != 0

    return wait_until(exited, timeout)
