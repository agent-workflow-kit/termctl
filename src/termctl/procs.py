"""Processes: a process's parent PID, start time and zombie state, for the recorded PID
(interface.md "Open order" step 7, "Liveness"; design.md "Internal architecture").

Linux and WSL read ``/proc/<pid>/stat``: field 3 is the state, field 4 the parent PID and field
22 the start time in clock ticks since boot. macOS runs ``ps -o ppid= -o stat= -o lstart=``
in the C locale with ``TZ=UTC0``; the start time is the ``lstart`` time in seconds since the
epoch (one-second resolution, interface.md "Platform notes"). The start time is an opaque
per-host token, compared only for equality.

Every read goes through the gateway. ``read`` returns None when the process does not exist,
and raises ``ProcessError`` when it cannot tell (an unreadable or malformed answer).

TODO(#23): the ``close --force`` signal sequence (``pidfd_open`` on Linux, design.md
"Implementation notes").
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from .gateway import Gateway

PS = "/bin/ps"
PS_TIMEOUT = 10.0
# ps's output must not depend on the caller's locale or time zone (lstart is local time).
PS_ENV = {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}
# The @termctl_pid grammar (interface.md "Pane options").
PID_MAX = 9_999_999_999
_NUMBER = re.compile(r"[0-9]{1,10}")
_START = re.compile(r"[0-9]{1,20}")
# Linux: Z zombie; X and x dead (being reaped).
_LINUX_GONE_STATES = frozenset({"Z", "X", "x"})
_LINUX_STATE = re.compile(r"[A-Za-z]")
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


class ProcessError(Exception):
    """The process could not be read: what it is, is unknown."""


@dataclass(frozen=True)
class Process:
    """A process as read: its parent PID, its start time (``@termctl_pid_start``) and
    whether it is a zombie (it has exited; a zombie counts as exited)."""

    ppid: int
    start: str
    zombie: bool


def read(gateway: Gateway, pid: int) -> Process | None:
    """The process ``pid``, or None when no such process exists."""
    if not 1 <= pid <= PID_MAX:
        raise ValueError(f"PID out of range: {pid}")
    if gateway.platform() == "darwin":
        return _read_ps(gateway, pid)
    return _read_proc(gateway, pid)


def _read_proc(gateway: Gateway, pid: int) -> Process | None:
    try:
        text = gateway.read_proc_stat(pid)
    except OSError as exc:
        raise ProcessError(f"cannot read /proc/{pid}/stat: {exc.strerror or exc}") from None
    if text is None:
        return None
    # The command name is in parentheses and may contain anything, ")" and spaces included:
    # the fields start after the last ")".
    head, close, rest = text.rpartition(")")
    fields = rest.split()
    if (
        not close
        or head.split(" (", 1)[0] != str(pid)
        or len(fields) < 20
        or not _LINUX_STATE.fullmatch(fields[0])
        or not _NUMBER.fullmatch(fields[1])
        or not _START.fullmatch(fields[19])
    ):
        raise ProcessError(f"unexpected contents of /proc/{pid}/stat")
    return Process(ppid=int(fields[1]), start=fields[19], zombie=fields[0] in _LINUX_GONE_STATES)


def _read_ps(gateway: Gateway, pid: int) -> Process | None:
    """macOS. Not exercised by CI (architecture: macOS is checked manually; #26)."""
    argv = [PS, "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", str(pid)]
    try:
        result = gateway.run(argv, timeout=PS_TIMEOUT, env=PS_ENV)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessError(f"cannot run {PS} for process {pid}: {exc}") from None
    text = result.stdout.strip()
    if result.returncode != 0 and not text:
        return None  # ps found no such process
    lines = text.splitlines()
    tokens = text.split()
    if result.returncode != 0 or len(lines) != 1 or len(tokens) != 7:
        raise ProcessError(f"unexpected output of {PS} for process {pid}")
    ppid, stat, lstart = tokens[0], tokens[1], " ".join(tokens[2:])
    if not _NUMBER.fullmatch(ppid):
        raise ProcessError(f"unexpected output of {PS} for process {pid}")
    try:
        started = datetime.strptime(lstart, _LSTART_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ProcessError(f"unexpected start time from {PS} for process {pid}") from None
    return Process(ppid=int(ppid), start=str(int(started.timestamp())), zombie=stat.startswith("Z"))
