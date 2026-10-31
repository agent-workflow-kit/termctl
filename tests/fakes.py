"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from termctl.gateway import FileInfo

UID = 1000


def directory(mode: int = 0o755, uid: int = 0) -> FileInfo:
    return FileInfo(mode=stat.S_IFDIR | mode, uid=uid)


def socket_file(mode: int = 0o600, uid: int = UID) -> FileInfo:
    return FileInfo(mode=stat.S_IFSOCK | mode, uid=uid)


def regular_file(mode: int = 0o644, uid: int = UID) -> FileInfo:
    return FileInfo(mode=stat.S_IFREG | mode, uid=uid)


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


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
        *,
        cwd: str | None = "/",
        dirs: set[str] | None = None,
        tty: bool = False,
        user: str | None = "user",
        uid: int = UID,
        files: Mapping[str, FileInfo] | None = None,
        links: Mapping[str, str] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []
        self.cwd = cwd
        # By default only the caller's current directory exists.
        if dirs is None:
            dirs = {cwd} if cwd is not None else set()
        self.dirs = set(dirs)
        self.tty = tty
        self.user = user
        self.dir_checks: list[str] = []
        self.uid = uid
        # A fake filesystem for lstat and real_path: absolute normalized paths to their
        # FileInfo, and symlinks to their targets. By default a safe default server exists.
        self.files = dict(safe_default_server(uid) if files is None else files)
        self.links = dict(links or {})
        self.lstats: list[str] = []

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        try:
            return self.responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway call: {argv!r}") from None

    def current_dir(self) -> str:
        if self.cwd is None:
            raise FileNotFoundError("current directory removed")
        return self.cwd

    def is_dir(self, path: str) -> bool:
        self.dir_checks.append(path)
        return path in self.dirs

    def stdin_isatty(self) -> bool:
        return self.tty

    def user_name(self) -> str | None:
        return self.user

    def real_uid(self) -> int:
        return self.uid

    def lstat(self, path: str) -> FileInfo:
        self.lstats.append(path)
        if path in self.links:
            return FileInfo(mode=stat.S_IFLNK | 0o777, uid=self.uid)
        if path in self.files:
            return self.files[path]
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)

    def real_path(self, path: str) -> str:
        return self._resolve(path, depth=0)

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
