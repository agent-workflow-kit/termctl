"""The per-server lock (interface.md "Audit and log", design.md D20).

``rename``, ``mark``, ``unmark``, ``label add`` and ``label remove`` hold an exclusive
``flock`` on ``locks/<first 16 hex digits of sha256(socket_path)>.lock`` in the state
directory while they read and rewrite pane options. The wait is at most 10 seconds, then exit
12 ``locked`` with ``details.cause: timeout``; a state directory, lock file or ``flock`` that
cannot be used exits 12 ``locked`` with ``cause: unusable``, ``path`` and ``problem``. The
kernel releases a ``flock`` when its holder dies, so a lock file left by a dead process is
simply locked again: there is no stale-lock recovery to do, and termctl never deletes lock
files. Should one be deleted anyway (by hand), a lock taken on the deleted file would not
exclude a process that creates a new file of the same name; so after each ``flock`` the
descriptor is compared (device and inode) with what the name points to now, and a mismatch
drops the descriptor and opens the name again. The lock is never held while hooks run;
callers keep the ``with`` block around the read-modify-write only, and the hook runner
(``hooks.py``) refuses to run while ``held()`` is true. The lock descriptor is
close-on-exec, so no child inherits it.
"""

# pane_change.change_pane takes the lock for rename (and, through it, #20 mark/unmark and
# #21 labels), with environ and clock from the CLI context.

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


def _open_locks_dir(environ: Mapping[str, str]) -> tuple[int, str]:
    state_fd, state_path = open_state_dir(environ)
    try:
        return open_subdir(state_fd, state_path, LOCKS_DIR), os.path.join(state_path, LOCKS_DIR)
    finally:
        os.close(state_fd)


def _is_current(fd: int, locks_fd: int, name: str) -> bool:
    """Whether ``name`` in ``locks_fd`` still names the file open as ``fd``."""
    held_file = os.fstat(fd)
    try:
        named = os.stat(name, dir_fd=locks_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (named.st_dev, named.st_ino) == (held_file.st_dev, held_file.st_ino)


def _timed_out(socket_path: str, timeout: float) -> TermctlError:
    return TermctlError(
        "locked",
        f"another termctl holds the lock for this server; gave up after {timeout:g} s",
        details={"socket_path": socket_path, "cause": "timeout"},
    )


def _unusable(socket_path: str, path: str | None, problem: str, message: str) -> TermctlError:
    details: dict[str, str] = {"socket_path": socket_path, "cause": "unusable"}
    if path is not None:
        details["path"] = path
    details["problem"] = problem
    return TermctlError("locked", f"cannot use the server lock: {message}", details=details)


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

    Raises ``TermctlError("locked")`` after waiting ``timeout`` seconds (``cause: timeout``),
    or when the state directory, the lock file or ``flock`` cannot be used (``cause:
    unusable``). Taking a second server lock in the same process is a programming error (it
    would wait on itself).
    """
    global _held
    if _held:
        raise RuntimeError("a server lock is already held by this process")
    name = lock_name(socket_path)
    deadline = clock.monotonic() + timeout
    fd = -1
    try:
        try:
            locks_fd, locks_path = _open_locks_dir(environ)
        except StateDirError as exc:
            raise _unusable(socket_path, exc.path, exc.problem, str(exc)) from None
        path = os.path.join(locks_path, name)
        reopened = 0
        try:
            while True:
                if fd < 0:
                    fd = open_file(locks_fd, locks_path, name, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    remaining = deadline - clock.monotonic()
                    if remaining <= 0:
                        raise _timed_out(socket_path, timeout) from None
                    sleep(min(POLL_S, remaining))
                    continue
                except OSError as exc:
                    problem = f"cannot lock: {exc.strerror or exc}"
                    raise _unusable(socket_path, path, problem, f"{path}: {problem}") from None
                if _is_current(fd, locks_fd, name):
                    break
                # The name was deleted or replaced: this lock excludes nobody. Open the
                # name again (at least once, then until the deadline).
                os.close(fd)
                fd = -1
                reopened += 1
                if reopened > 1 and clock.monotonic() >= deadline:
                    raise _timed_out(socket_path, timeout)
        except StateDirError as exc:
            raise _unusable(socket_path, exc.path, exc.problem, str(exc)) from None
        except OSError as exc:
            problem = exc.strerror or str(exc)
            raise _unusable(socket_path, path, problem, f"{path}: {problem}") from None
        finally:
            os.close(locks_fd)
        _held = True
        try:
            yield
        finally:
            _held = False
    finally:
        # Closing the only descriptor releases the flock.
        if fd >= 0:
            os.close(fd)
