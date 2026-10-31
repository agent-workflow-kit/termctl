"""Processes: a process's parent PID, start time and zombie state, for the recorded PID
(interface.md "Open order" step 7, "Liveness"; design.md "Internal architecture").

Linux and WSL read ``/proc/<pid>/stat``: field 3 is the state, field 4 the parent PID and field
22 the start time in clock ticks since boot. macOS runs ``ps -o ppid= -o stat= -o lstart=``
in the C locale with ``TZ=UTC0``; the start time is the ``lstart`` time in seconds since the
epoch (one-second resolution, interface.md "Platform notes"). The start time is an opaque
per-host token, compared only for equality.

Every read goes through the gateway. ``read`` returns None when the process does not exist,
and raises ``ProcessError`` when it cannot tell (an unreadable or malformed answer).

``close --force`` (interface.md ``close`` steps 3 and 5) proves a recorded PID is the pane's
process with ``verify`` before it signals, and signals it with ``signal_sequence``: through a
pidfd on Linux, so a PID reused after the handle was opened is never signaled, and with the
start time checked before every signal (design.md "Implementation notes"). Only that one
process is ever signaled, never a process group.
"""

from __future__ import annotations

import enum
import re
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .gateway import Clock, Gateway, ProcessHandle

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


def namespace_problem(gateway: Gateway) -> str | None:
    """Linux: why ``/proc`` cannot be trusted to show the processes termctl's PID namespace
    sees (a pidfd and ``SO_PEERCRED`` use that namespace; ``/proc`` shows the one it was
    mounted for, e.g. after ``unshare -p`` without a new ``/proc``, or a host ``/proc`` bound
    into a container), or None when it can: ``/proc/self`` must name termctl's own PID."""
    try:
        if gateway.proc_is_own_namespace():
            return None
    except OSError as err:
        return f"cannot read /proc/self: {err.strerror or err}"
    return "/proc belongs to another PID namespace than termctl"


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
    if result.returncode != 0 and not text and not result.stderr.strip():
        return None  # ps found no such process (it says nothing then)
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
    start = str(int(started.timestamp()))
    if not _START.fullmatch(start):  # before the epoch: outside the @termctl_pid_start grammar
        raise ProcessError(f"unexpected start time from {PS} for process {pid}")
    return Process(ppid=int(ppid), start=start, zombie=stat.startswith("Z"))


# --- close --force: proving the process and signaling it ---------------------------------------


class Identity(enum.Enum):
    """What ``verify`` found for a recorded PID."""

    # The live process with the recorded start time, whose parent is the server.
    VERIFIED = "verified"
    # No such process, or a zombie: it has exited, so there is nothing to signal.
    GONE = "gone"
    # Not the pane's process: another start time, another parent, or a server outside
    # termctl's PID namespace. Never signaled.
    MISMATCH = "mismatch"
    # The check could not be made; never signaled. ``problem`` says why.
    UNCHECKABLE = "uncheckable"


@dataclass(frozen=True)
class Verification:
    identity: Identity
    problem: str | None = None


def verify(
    gateway: Gateway, socket_path: str, pid: int, start: str, server_pid: int
) -> Verification:
    """Prove that ``pid`` is the recorded pane process before ``close --force`` signals it:
    the checks of interface.md "Open order" step 7, repeated in termctl's own namespace.

    On Linux the server socket's ``SO_PEERCRED`` PID must be ``server_pid`` (tmux's
    ``#{pid}``): tmux reports PIDs in the server's PID namespace and ``/proc`` is read in
    termctl's, which ``/proc`` must show (``namespace_problem``). Then the process must exist,
    not be a zombie, have the recorded ``start`` time and have the server as its parent. A
    check that cannot be made is ``UNCHECKABLE``.
    """
    if gateway.platform() != "darwin":
        try:
            peer = gateway.socket_peer_pid(socket_path)
        except OSError as err:
            return Verification(
                Identity.UNCHECKABLE, f"cannot check the server: {err.strerror or err}"
            )
        if peer != server_pid:
            return Verification(Identity.MISMATCH)
        problem = namespace_problem(gateway)
        if problem is not None:
            return Verification(Identity.UNCHECKABLE, problem)
    try:
        process = read(gateway, pid)
    except ProcessError as err:
        return Verification(Identity.UNCHECKABLE, str(err))
    if process is None or process.zombie:
        return Verification(Identity.GONE)
    if process.start != start or process.ppid != server_pid:
        return Verification(Identity.MISMATCH)
    return Verification(Identity.VERIFIED)


# interface.md `close` step 5: SIGTERM after the grace period, SIGKILL 5 s later, then up to
# 1 s for the process to exit (spike F3).
TERM_WAIT_S = 5.0
KILL_WAIT_S = 1.0
# How often the waits look at the process.
POLL_S = 0.05


class _State(enum.Enum):
    ALIVE = "alive"
    EXITED = "exited"
    # Alive with another start time: not the recorded process.
    MISMATCH = "mismatch"
    # It cannot be read: stop signaling.
    UNKNOWN = "unknown"


@dataclass
class SignalOutcome:
    """The ``close`` output fields of a signal sequence: ``signals_sent`` (``SIGTERM``, then
    ``SIGKILL``), ``exited`` (None with ``pid_mismatch``) and warnings to print."""

    signals_sent: list[str] = field(default_factory=list[str])
    exited: bool | None = True
    pid_mismatch: bool = False
    warnings: list[str] = field(default_factory=list[str])


def signal_sequence(
    gateway: Gateway,
    clock: Clock,
    handle: ProcessHandle,
    pid: int,
    start: str,
    grace_s: float,
    sent: list[str] | None = None,
) -> SignalOutcome:
    """interface.md `close` step 5, after the kernel's SIGHUP (the pane was killed, or was
    found gone): wait up to ``grace_s`` for the process to exit, send SIGTERM, wait 5 seconds,
    send SIGKILL, wait up to 1 second, stopping as soon as it has exited.

    ``handle`` was opened on ``pid`` before its identity was proven (``verify``), so it names
    the recorded process. Before each signal the process must still have the recorded
    ``start`` time; another start time before the first signal is ``pid_mismatch`` (nothing
    is sent), and after a signal means the recorded process has exited and its PID was
    reused. A process that cannot be read stops the sequence with a warning; it is reported
    as exited only if the handle says so. A process that survives SIGKILL is ``exited:
    False``. ``sent``, when given, is the outcome's ``signals_sent`` list, so the caller sees
    each signal as soon as it is sent (a run stopped meanwhile audits them).
    """
    outcome = SignalOutcome() if sent is None else SignalOutcome(signals_sent=sent)

    def state() -> _State:
        if handle.exited():
            return _State.EXITED
        try:
            process = read(gateway, pid)
        except ProcessError as err:
            outcome.warnings.append(f"cannot check process {pid}: {err}; not signaling it")
            return _State.UNKNOWN
        if process is None or process.zombie:
            return _State.EXITED
        if process.start != start:
            return _State.MISMATCH
        return _State.ALIVE

    def wait(seconds: float) -> _State:
        deadline = clock.monotonic() + seconds
        while True:
            current = state()
            if current is not _State.ALIVE:
                return current
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                return current
            clock.sleep(min(POLL_S, remaining))

    for seconds, signum in (
        (grace_s, signal.SIGTERM),
        (TERM_WAIT_S, signal.SIGKILL),
        (KILL_WAIT_S, None),
    ):
        current = wait(seconds)
        if current is _State.MISMATCH and not outcome.signals_sent:
            outcome.pid_mismatch, outcome.exited = True, None
            return outcome
        if current in (_State.EXITED, _State.MISMATCH):
            return outcome  # exited (a reused PID means the recorded process is gone)
        if current is _State.UNKNOWN:
            outcome.exited = handle.exited()
            return outcome
        if signum is None:
            outcome.exited = False
            return outcome
        if not _send(handle, signum, pid, outcome.warnings.append):
            outcome.exited = handle.exited() or _gone(gateway, pid)
            return outcome
        outcome.signals_sent.append(signum.name)
    raise AssertionError("unreachable")  # pragma: no cover


def _send(
    handle: ProcessHandle, signum: signal.Signals, pid: int, warn: Callable[[str], None]
) -> bool:
    """Send ``signum``; whether it was sent. A process gone meanwhile is not a failure the
    caller reports; any other is a warning."""
    try:
        if handle.send_signal(signum):
            return True
    except OSError as err:
        warn(f"cannot send {signum.name} to process {pid}: {err.strerror or err}")
    return False


def _gone(gateway: Gateway, pid: int) -> bool:
    try:
        process = read(gateway, pid)
    except ProcessError:
        return False
    return process is None or process.zombie
