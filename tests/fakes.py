"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from termctl.gateway import BoundedOutput, FileInfo

UID = 1000


def directory(mode: int = 0o755, uid: int = 0) -> FileInfo:
    return FileInfo(mode=stat.S_IFDIR | mode, uid=uid)


def socket_file(mode: int = 0o600, uid: int = UID) -> FileInfo:
    return FileInfo(mode=stat.S_IFSOCK | mode, uid=uid)


def regular_file(mode: int = 0o644, uid: int = UID) -> FileInfo:
    return FileInfo(mode=stat.S_IFREG | mode, uid=uid)


def symlink(uid: int = UID) -> FileInfo:
    """The ``lstat`` of a symlink; put it in ``files`` to give a link in ``links`` an owner."""
    return FileInfo(mode=stat.S_IFLNK | 0o777, uid=uid)


def safe_default_server(uid: int = UID, tmpdir: str = "/tmp") -> dict[str, FileInfo]:
    """A filesystem with a safe default socket at ``<tmpdir>/tmux-<uid>/default``."""
    files = {"/": directory()}
    path = ""
    for part in tmpdir.strip("/").split("/"):
        path = f"{path}/{part}"
        files[path] = directory()
    files[path] = directory(0o1777)
    files[f"{path}/tmux-{uid}"] = directory(0o700, uid)
    files[f"{path}/tmux-{uid}/default"] = socket_file(uid=uid)
    return files


# The tmux binary the fakes know about by default, found on ``PATH=/usr/bin``.
TMUX = "/usr/bin/tmux"

# A scripted result: text for ``run``; text or bytes for ``run_bounded``.
Response = subprocess.CompletedProcess[Any] | BaseException


def completed(
    stdout: str | bytes = "", *, stderr: str | bytes = "", returncode: int = 0
) -> subprocess.CompletedProcess[Any]:
    """A scripted result of one ``run`` or ``run_bounded`` call."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _as_bytes(output: str | bytes) -> bytes:
    return output.encode() if isinstance(output, str) else output


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects. A
    scripted exception is raised instead of returned. ``tmux -V`` of ``TMUX`` answers
    ``tmux 3.5a`` unless the test scripts it, and ``TMUX`` is the only executable file
    unless the test gives ``executables``.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], Response] | None = None,
        *,
        cwd: str | None = "/",
        dirs: set[str] | None = None,
        tty: bool = False,
        user: str | None = "user",
        executables: set[str] | None = None,
        uid: int = UID,
        files: Mapping[str, FileInfo] | None = None,
        links: Mapping[str, str] | None = None,
    ) -> None:
        self.responses: dict[tuple[str, ...], Response] = {
            (TMUX, "-V"): completed("tmux 3.5a\n"),
            **(responses or {}),
        }
        self.executables = {TMUX} if executables is None else set(executables)
        self.executable_checks: list[str] = []
        self.calls: list[list[str]] = []
        # The ``env`` argument of each call, in the order of ``calls``.
        self.envs: list[dict[str, str] | None] = []
        self.uid = uid
        # A fake filesystem for lstat and real_path: absolute normalized paths to their
        # FileInfo, and symlinks to their targets (a link's owner is the uid unless ``files``
        # has a ``symlink`` entry for it). By default a safe default server exists.
        self.files = dict(safe_default_server(uid) if files is None else files)
        self.links = dict(links or {})
        self.lstats: list[str] = []
        self.cwd = cwd
        # By default only the caller's current directory exists.
        if dirs is None:
            dirs = {cwd} if cwd is not None else set()
        self.dirs = set(dirs)
        self.tty = tty
        self.user = user
        self.dir_checks: list[str] = []

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._respond(argv, env)

    def run_bounded(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        limit: int,
    ) -> BoundedOutput:
        """Answers like ``run``; output over ``limit`` is cut to it and marked truncated."""
        response = self._respond(argv, env)
        stdout, stderr = _as_bytes(response.stdout), _as_bytes(response.stderr)
        truncated = len(stdout) > limit or len(stderr) > limit
        returncode = -9 if truncated else response.returncode
        return BoundedOutput(returncode, stdout[:limit], stderr[:limit], truncated)

    def _respond(
        self, argv: list[str], env: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[Any]:
        self.calls.append(list(argv))
        self.envs.append(None if env is None else dict(env))
        try:
            response = self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway call: {argv!r}") from None
        if isinstance(response, BaseException):
            raise response
        return response

    def current_dir(self) -> str:
        if self.cwd is None:
            raise FileNotFoundError("current directory removed")
        return self.cwd

    def is_dir(self, path: str) -> bool:
        self.dir_checks.append(path)
        return path in self.dirs

    def is_executable_file(self, path: str) -> bool:
        self.executable_checks.append(path)
        return path in self.executables

    def real_uid(self) -> int:
        return self.uid

    def lstat(self, path: str) -> FileInfo:
        self.lstats.append(path)
        if path in self.links:
            return self.files.get(path, symlink(self.uid))
        if path in self.files:
            return self.files[path]
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    def real_path(self, path: str) -> str:
        return self._resolve(path, depth=0)

    def read_link(self, path: str) -> str:
        if path in self.links:
            return self.links[path]
        if path in self.files:
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), path)
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    def _resolve(self, path: str, *, depth: int) -> str:
        if depth > 40:
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)
        current = "/"
        for part in path.split("/"):
            if part in ("", "."):
                continue
            if current in self.files and not stat.S_ISDIR(self.files[current].mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), current)
            if part == "..":
                current = os.path.dirname(current)
                continue
            current = os.path.join(current, part)
            if current in self.links:
                target = os.path.join(os.path.dirname(current), self.links[current])
                current = self._resolve(target, depth=depth + 1)
            elif current not in self.files:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), current)
        return current

    def stdin_isatty(self) -> bool:
        return self.tty

    def user_name(self) -> str | None:
        return self.user


class FakeClock:
    """A clock that only moves when the test advances it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds
