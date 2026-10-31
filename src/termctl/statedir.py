"""The state directory: ``~/.local/state/termctl`` (respecting ``XDG_STATE_HOME``).

It holds ``log.jsonl``, ``audit.jsonl`` and ``locks/`` (interface.md "Audit and log", "State
directory"). Like config, it is accessed here rather than through the gateway (design.md
"Internal architecture"). Everything below the state directory is opened relative to a checked
directory descriptor with ``O_NOFOLLOW``, and every check is made on the opened descriptor,
so a symlink or a swapped entry cannot redirect a write (no check-then-use race). The state
directory itself may be a symlink (it is the user's choice of location), but what it resolves
to must pass the same checks.

Directories termctl creates (missing ancestors of the state directory included) are 0700 and
files 0600, whatever the umask. termctl creates nothing under a nearest existing ancestor that
another user owns (e.g. root under ``sudo -E`` in the user's home). Ancestors that already
exist are not checked: a user with umask 002 commonly has ``~/.local`` at 0775.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .config import xdg_base

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO planted in place of a file must not block termctl.
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class StateDirError(Exception):
    """The state directory or a file in it cannot be used safely.

    ``path`` is the path concerned (None when none could be determined), ``problem`` what is
    wrong with it; ``hint`` an optional fix for the message."""

    def __init__(self, path: str | None, problem: str, hint: str | None = None) -> None:
        message = problem if path is None else f"{path}: {problem}"
        if hint is not None:
            message += f"; {hint}"
        super().__init__(message)
        self.path = path
        self.problem = problem


def state_dir(environ: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/termctl`` when set and absolute, else ``~/.local/state/termctl``."""
    base = xdg_base(environ, "XDG_STATE_HOME", ".local/state")
    if base is None:
        raise StateDirError(None, "cannot determine the home directory; set HOME or XDG_STATE_HOME")
    return base / "termctl"


def _shell_quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def _check(fd: int, path: str, *, directory: bool) -> None:
    """Raise unless ``fd`` is a directory (or a regular file with one link) owned by the
    effective user and not writable by group or others. Closes ``fd`` before raising."""
    info = os.fstat(fd)
    problem = None
    hint = None
    writable = info.st_mode & 0o022
    if directory and not stat.S_ISDIR(info.st_mode):
        problem = "not a directory"
    elif not directory and not stat.S_ISREG(info.st_mode):
        problem = "not a regular file"
    elif info.st_uid != os.geteuid():
        problem = "not owned by the current user"
    elif writable:
        who = {0o020: "group", 0o002: "others", 0o022: "group and others"}[writable]
        problem = f"writable by {who} (mode {stat.S_IMODE(info.st_mode):04o})"
        hint = f"run: chmod go-w {_shell_quote(path)}"
    elif not directory and info.st_nlink != 1:
        problem = f"has {info.st_nlink} hard links"
    if problem is not None:
        os.close(fd)
        raise StateDirError(path, problem, hint)


def _is_symlink(name: str | Path, dir_fd: int | None) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def _os_error(
    path: str, exc: OSError, name: str | Path | None = None, dir_fd: int | None = None
) -> StateDirError:
    """``exc`` from opening ``name`` (in ``dir_fd``) as a ``StateDirError`` for ``path``."""
    if exc.errno == errno.ELOOP or (
        exc.errno == errno.ENOTDIR and name is not None and _is_symlink(name, dir_fd)
    ):
        return StateDirError(path, "is a symlink")
    if exc.errno == errno.ENOTDIR:
        return StateDirError(path, "not a directory")
    return StateDirError(path, exc.strerror or str(exc))


def _create_dirs(path: Path) -> None:
    """Create ``path`` and its missing ancestors with mode 0700, refusing when the nearest
    existing ancestor is not owned by the effective user.

    Each missing component is created and opened relative to its parent's descriptor with
    ``O_NOFOLLOW``, and its mode fixed on that descriptor, so a component swapped for a
    symlink is refused rather than followed."""
    missing: list[Path] = []
    existing = path
    while True:
        try:
            info = os.stat(existing)
            break
        except FileNotFoundError:
            missing.append(existing)
            existing = existing.parent
    if not missing:
        return
    if info.st_uid != os.geteuid():
        raise StateDirError(str(path), f"parent {existing} not owned by the current user")
    try:
        parent_fd = os.open(existing, _DIR_FLAGS)
    except OSError as exc:
        raise _os_error(str(existing), exc) from None
    try:
        for directory in reversed(missing):
            name = directory.name
            created = True
            try:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    created = False  # a concurrent creator; checked when opened
                fd = os.open(name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as exc:
                raise _os_error(str(directory), exc, name, parent_fd) from None
            if created:
                os.fchmod(fd, 0o700)  # the umask may have narrowed it
            os.close(parent_fd)
            parent_fd = fd
    finally:
        os.close(parent_fd)


def open_state_dir(environ: Mapping[str, str]) -> tuple[int, str]:
    """Create the state directory if missing and return a checked descriptor for it and its
    path. The caller closes the descriptor."""
    path = state_dir(environ)
    try:
        _create_dirs(path)
        fd = os.open(path, _DIR_FLAGS)
    except OSError as exc:
        raise _os_error(str(path), exc) from None
    _check(fd, str(path), directory=True)
    return fd, str(path)


def open_subdir(dir_fd: int, dir_path: str, name: str) -> int:
    """Create ``name`` (mode 0700) in ``dir_fd`` (whose path is ``dir_path``) if missing and
    return a checked descriptor for it; a symlink is refused. The caller closes it."""
    path = os.path.join(dir_path, name)
    created = True
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            created = False
        fd = os.open(name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise _os_error(path, exc, name, dir_fd) from None
    _check(fd, path, directory=True)
    if created:
        os.fchmod(fd, 0o700)  # the umask may have narrowed it
    return fd


def open_file(dir_fd: int, dir_path: str, name: str, flags: int) -> int:
    """Open (creating with mode 0600) the regular file ``name`` in ``dir_fd`` (whose path is
    ``dir_path``) with ``flags`` added; a symlink, FIFO, directory, device or hard-linked file
    is refused. The caller closes it."""
    path = os.path.join(dir_path, name)
    created = True
    try:
        try:
            fd = os.open(name, flags | _FILE_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            created = False
            fd = os.open(name, flags | _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _os_error(path, exc, name, dir_fd) from None
    _check(fd, path, directory=False)
    if created:
        os.fchmod(fd, 0o600)  # the umask may have narrowed it
    # Blocking writes from here on; the descriptor is known to be a regular file.
    os.set_blocking(fd, True)
    return fd
