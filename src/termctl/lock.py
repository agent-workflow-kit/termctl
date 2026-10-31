"""The per-server lock (interface.md "Audit and log", design.md D20).

``rename``, ``mark``, ``unmark``, ``label add`` and ``label remove`` hold an exclusive
``flock`` on ``locks/<first 16 hex digits of sha256(socket_path)>.lock`` in the state
directory while they read and rewrite pane options. The wait is at most 10 seconds, then exit
12 ``locked``. The kernel releases a ``flock`` when its holder dies, so a lock file left by a
dead process is simply locked again: there is no stale-lock recovery to do, and lock files are
never deleted (deleting one would let two processes lock different files of the same name).
The lock is never held while hooks run; callers keep the ``with`` block around the
read-modify-write only, and ``held()`` lets the hook runner assert it.
"""

# TODO(#6): hooks.py refuses to run a hook while held() is true.
# TODO(#19): rename (then #20 mark/unmark, #21 labels) wraps its read-modify-write in
# server_lock, with environ and clock from the CLI context.

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

from .errors import TermctlError
from .gateway import Clock
from .statedir import StateDirError, open_file, open_state_dir, open_subdir

TIMEOUT_S = 10.0
POLL_S = 0.05
LOCKS_DIR = "locks"

_held = False


def held() -> bool:
    """Whether this process holds a server lock."""
    return _held


def lock_name(socket_path: str) -> str:
    """The lock file name for a canonical (absolute) ``socket_path``."""
    if not socket_path.startswith("/"):
        raise ValueError(f"socket_path must be absolute: {socket_path!r}")
    digest = hashlib.sha256(socket_path.encode("utf-8")).hexdigest()
    return f"{digest[:16]}.lock"


def _open_lock_file(environ: Mapping[str, str], name: str) -> int:
    state_fd = open_state_dir(environ)
    try:
        locks_fd = open_subdir(state_fd, LOCKS_DIR)
    finally:
        os.close(state_fd)
    try:
        return open_file(locks_fd, name, os.O_RDWR)
    finally:
        os.close(locks_fd)


def _locked(socket_path: str, message: str) -> TermctlError:
    return TermctlError("locked", message, details={"socket_path": socket_path})


@contextmanager
def server_lock(
    socket_path: str,
    *,
    environ: Mapping[str, str],
    clock: Clock,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TIMEOUT_S,
) -> Iterator[None]:
    """Hold the exclusive lock for ``socket_path`` for the body of the ``with`` block.

    Raises ``TermctlError("locked")`` after waiting ``timeout`` seconds, or when the lock file
    cannot be used safely. Taking a second server lock in the same process is a programming
    error (it would wait on itself).
    """
    global _held
    if _held:
        raise RuntimeError("a server lock is already held by this process")
    name = lock_name(socket_path)
    try:
        fd = _open_lock_file(environ, name)
    except StateDirError as exc:
        raise _locked(socket_path, f"cannot use the server lock: {exc}") from None
    try:
        deadline = clock.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    raise _locked(
                        socket_path,
                        f"another termctl holds the lock for this server; gave up after "
                        f"{timeout:g} s",
                    ) from None
                sleep(min(POLL_S, remaining))
        _held = True
        try:
            yield
        finally:
            _held = False
    finally:
        # Closing the only descriptor releases the flock.
        os.close(fd)
