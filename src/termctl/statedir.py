"""The state directory: ``~/.local/state/termctl`` (respecting ``XDG_STATE_HOME``).

It holds ``log.jsonl``, ``audit.jsonl`` and ``locks/`` (interface.md "Audit and log"). Like
config, it is accessed here rather than through the gateway (design.md "Internal
architecture"). Everything below the state directory is opened relative to a checked
directory descriptor with ``O_NOFOLLOW``, and every check is made on the opened descriptor,
so a symlink or a swapped entry cannot redirect a write (no check-then-use race). The state
directory itself may be a symlink (it is the user's choice of location), but what it resolves
to must be a directory owned by the effective user and not writable by group or others.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .config import xdg_base

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO planted in place of a file must not block termctl.
_FILE_FLAGS = os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class StateDirError(Exception):
    """The state directory or a file in it cannot be used safely."""


def state_dir(environ: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/termctl`` when set and absolute, else ``~/.local/state/termctl``."""
    base = xdg_base(environ, "XDG_STATE_HOME", ".local/state")
    if base is None:
        raise StateDirError("cannot determine the home directory; set HOME or XDG_STATE_HOME")
    return base / "termctl"


def _check(fd: int, what: str, *, directory: bool) -> None:
    """Raise unless ``fd`` is a directory (or regular file) owned by the effective user and
    not writable by group or others. Closes ``fd`` before raising."""
    info = os.fstat(fd)
    problem = None
    if directory and not stat.S_ISDIR(info.st_mode):
        problem = "not a directory"
    elif not directory and not stat.S_ISREG(info.st_mode):
        problem = "not a regular file"
    elif info.st_uid != os.geteuid():
        problem = "not owned by the current user"
    elif info.st_mode & 0o022:
        problem = "writable by others"
    if problem is not None:
        os.close(fd)
        raise StateDirError(f"{what}: {problem}")


def open_state_dir(environ: Mapping[str, str]) -> int:
    """Create the state directory (mode 0700) if missing and return a checked descriptor for
    it. The caller closes it."""
    path = state_dir(environ)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, _DIR_FLAGS)
    except OSError as exc:
        raise StateDirError(f"{path}: {exc.strerror or exc}") from None
    _check(fd, str(path), directory=True)
    return fd


def _name(dir_fd: int, name: str) -> str:
    """``name`` for messages: the directory's path when the platform can tell it."""
    try:
        return os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), name)
    except OSError:
        return name


def open_subdir(dir_fd: int, name: str) -> int:
    """Create ``name`` (mode 0700) in ``dir_fd`` if missing and return a checked descriptor
    for it; a symlink is refused. The caller closes it."""
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
        fd = os.open(name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise StateDirError(f"{_name(dir_fd, name)}: {exc.strerror or exc}") from None
    _check(fd, _name(dir_fd, name), directory=True)
    return fd


def open_file(dir_fd: int, name: str, flags: int) -> int:
    """Open (creating with mode 0600) the regular file ``name`` in ``dir_fd`` with ``flags``
    added; a symlink, FIFO, directory or device is refused. The caller closes it."""
    try:
        fd = os.open(name, flags | _FILE_FLAGS, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise StateDirError(f"{_name(dir_fd, name)}: {exc.strerror or exc}") from None
    _check(fd, _name(dir_fd, name), directory=False)
    # Blocking writes from here on; the descriptor is known to be a regular file.
    os.set_blocking(fd, True)
    return fd
