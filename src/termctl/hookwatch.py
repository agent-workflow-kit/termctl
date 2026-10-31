"""The watchdog of an async post-hook (interface.md "Hooks").

``Gateway.spawn_watched`` starts this file detached, in a session of its own, by its absolute
path as ``python -I /path/to/hookwatch.py TIMEOUT ARGV...``, with the hook's payload on stdin,
its output discarded and exactly the hook's environment. ``-I`` keeps the caller's current
directory and ``PYTHON*`` variables from choosing the code that runs here; running by path means
the interpreter need not be able to import termctl (a user-site install, PYTHONPATH). So this
file imports only the standard library, never termctl.

The watchdog starts ARGV in a new process group, hands its own stdin over unread and waits at
most TIMEOUT seconds. On timeout, or when it gets SIGTERM, SIGHUP or SIGINT itself, it sends
SIGKILL to the hook's process group and to the hook's leader while the leader is still
unreaped (until then the leader's PID, the group's id, cannot be reused), then reaps the leader
and exits. A stop signal that comes while the hook starts takes effect once the watchdog has the
hook's process, so the hook is killed then. A hook that exits in time is not touched further:
what it left in its group keeps running.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import FrameType

# How long to wait for a leader that was sent SIGKILL to be reaped.
REAP_WAIT_S = 5.0
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def kill_unreaped(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the process group of ``proc`` and ``proc`` itself, if ``proc`` is unreaped.

    ``proc`` must have been started in a new process group (``process_group=0``). Its PID is
    signaled too because the leader may have moved itself into another group. Errors are
    ignored: the caller reaps ``proc`` and reports whether that worked.
    """
    if proc.returncode is not None:
        return
    for kill in (os.killpg, os.kill):
        try:
            kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass


class _Stop(BaseException):
    """The watchdog itself got a stop signal."""


_stopping = False
# While the hook starts, a stop signal is held (not raised) until _release. Not blocked with
# pthread_sigmask: the hook would inherit the blocked mask.
_held = False
_pending = False


def _stop(signum: int, frame: FrameType | None) -> None:
    # Only the first stop signal raises, so a second one cannot interrupt the cleanup.
    global _stopping, _pending
    if _stopping:
        return
    _stopping = True
    if _held:
        _pending = True
        return
    raise _Stop


def _release() -> None:
    """Stop holding stop signals; raise ``_Stop`` for one that came while held."""
    global _held, _pending
    _held = False
    if _pending:
        _pending = False
        raise _Stop


def main(argv: list[str]) -> int:
    """``argv`` is TIMEOUT followed by the hook's argv; exit 2 if it is not."""
    try:
        timeout = float(argv[0])
    except (IndexError, ValueError):
        return 2
    command = argv[1:]
    if not command or not timeout > 0:
        return 2
    # An inherited SIG_IGN would let the kernel reap the hook, so its PID could be reused
    # before the group is signaled.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    global _held
    _held = True
    for signum in _STOP_SIGNALS:
        signal.signal(signum, _stop)
    try:
        proc = subprocess.Popen(
            command,
            stdin=None,  # fd 0, the payload file, handed over unread
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            process_group=0,
        )
    except (OSError, ValueError):
        return 127  # nothing started, so nothing to kill, stopped or not
    try:
        _release()
        proc.wait(timeout)
        return 0
    except (subprocess.TimeoutExpired, _Stop):
        pass
    for signum in _STOP_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)
    kill_unreaped(proc)
    try:
        proc.wait(REAP_WAIT_S)
    except subprocess.TimeoutExpired:
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
