"""Test doubles for the gateway and the clock."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from termctl import panes as panes_module
from termctl.gateway import BoundedOutput, FileInfo, GroupRun

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


UID = 1000

# One managed pane as ``list-panes`` prints it for the pane reader's format (``panes.FIELDS``):
# running, in session ``termctl``, created 2026-10-30T12:00:00.000Z.
PANE_VALUES: dict[str, str] = {
    "pane_id": "%3",
    "window_id": "@2",
    "pane_dead": "0",
    "pane_dead_status": "",
    "pane_dead_signal": "",
    "pane_pid": "4242",
    "window_panes": "1",
    "session_name": "termctl",
    "window_name": "KEY-12",
    "@termctl_pane": "%3",
    "@termctl_v": "1",
    "@termctl_window": "@2",
    "@termctl_id": "tc-abcdefgh",
    "@termctl_name": "KEY-12",
    "@termctl_marker": "",
    "@termctl_marker_text": "",
    "@termctl_labels": "kind=review",
    "@termctl_placement": "default",
    "@termctl_keep": "never",
    "@termctl_created": "2026-10-30T12:00:00.000Z",
    "@termctl_pid": "4242",
    "@termctl_pid_start": "123456",
}


def pane_row(
    pane_id: str = "%3", tc_id: str = "tc-abcdefgh", overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The ``list-panes`` values of a managed pane with tmux pane id ``pane_id``."""
    return {
        **PANE_VALUES,
        "pane_id": pane_id,
        "@termctl_pane": pane_id,
        "@termctl_id": tc_id,
        **(overrides or {}),
    }


def list_panes_stdout(rows: Sequence[Mapping[str, str]], sentinel: str) -> str:
    """What ``list-panes -a -F <the reader's format>`` prints for ``rows``."""
    return "".join(
        "\t".join([*(row[name] for name in panes_module.FIELDS), sentinel]) + "\n" for row in rows
    )


def is_list_panes(argv: Sequence[str]) -> bool:
    """Whether ``argv`` is the pane reader's ``list-panes -a -F`` call."""
    return len(argv) >= 4 and list(argv[-4:-1]) == ["list-panes", "-a", "-F"]


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


# The fields of /proc/<pid>/stat between the parent PID (field 4) and the start time (22).
_STAT_MIDDLE = " ".join(["0"] * 17)


@dataclass(eq=False)
class FakeProcess:
    """A process for ``close --force``: ``read_proc_stat`` and ``open_process`` answer from it.

    It exits (and is reaped) on a signal in ``exits_on``, at clock time ``exit_at``, or, with
    ``hup_on_kill``, when the fake tmux runs a ``kill-pane`` (the kernel's SIGHUP). ``zombie``
    keeps it as a zombie instead of reaping it. ``signals`` lists what it received.
    """

    pid: int = 4242
    start: str = "123456"
    ppid: int = 77
    exits_on: frozenset[int] = frozenset()
    exit_at: float | None = None
    hup_on_kill: bool = False
    zombie: bool = False
    exited: bool = False
    signals: list[int] = field(default_factory=list[int])
    clock: FakeClock | None = None

    def done(self) -> bool:
        if not self.exited and self.exit_at is not None and self.clock is not None:
            self.exited = self.clock.monotonic() >= self.exit_at
        return self.exited

    def stat(self) -> str | None:
        if self.done() and not self.zombie:
            return None
        state = "Z" if self.done() else "S"
        return f"{self.pid} (sh) {state} {self.ppid} {_STAT_MIDDLE} {self.start} 0 0\n"


class FakeHandle:
    """A pidfd on one ``FakeProcess``: a later process with the same PID never receives its
    signals."""

    def __init__(self, process: FakeProcess, log: list[tuple[int, int]]) -> None:
        self.process = process
        self.log = log
        self.closed = False
        self.error: OSError | None = None

    def exited(self) -> bool:
        assert not self.closed
        return self.process.done()

    def send_signal(self, signum: int) -> bool:
        assert not self.closed
        if self.error is not None:
            raise self.error
        if self.process.done():
            return False
        self.process.signals.append(signum)
        self.log.append((self.process.pid, signum))
        if signum in self.process.exits_on:
            self.process.exited = True
        return True

    def close(self) -> None:
        self.closed = True


@dataclass
class GroupCall:
    """One ``run_group`` or ``spawn_watched`` call as the fake saw it."""

    argv: list[str]
    stdin: bytes
    env: dict[str, str]
    timeout: float | None
    stdout_limit: int | None
    stderr_limit: int | None
    spawned: bool


# A scripted group run: a fixed result, an exception to raise (``KeyboardInterrupt`` and
# ``Terminated`` included), or a function of the call.
GroupResponse = GroupRun | BaseException | Callable[[GroupCall], GroupRun]


def group_run(
    returncode: int | None = 0, stdout: bytes = b"", stderr: bytes = b"", pgid: int = 4242
) -> GroupRun:
    return GroupRun(pgid, returncode, stdout, stderr, False, False)


class FakeGateway:
    """Records every call and answers from a table of scripted results.

    An unscripted call fails the test, so a test states every external call it expects. A
    scripted exception is raised instead of returned. ``tmux -V`` of ``TMUX`` answers
    ``tmux 3.5a`` unless the test scripts it, and ``TMUX`` is the only executable file
    unless the test gives ``executables``.

    The pane reader's ``list-panes`` carries a fresh random sentinel, so it cannot be scripted
    by argv: ``panes`` gives the rows it prints (``pane_row``), with the sentinel taken from
    the call, and ``list_panes`` a fixed result instead (a failure, say). With neither, it is
    an unscripted call.
    """

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], Response] | None = None,
        *,
        cwd: str | None = "/",
        dirs: set[str] | None = None,
        tty: bool = False,
        user: str | None = "user",
        uid: int = UID,
        files: Mapping[str, FileInfo] | None = None,
        links: Mapping[str, str] | None = None,
        executables: set[str] | None = None,
        panes: Sequence[Mapping[str, str]] | None = None,
        list_panes: Response | None = None,
        mkdir_error: OSError | None = None,
        system: str = "linux",
        proc_stats: Mapping[int, str | None | BaseException] | None = None,
        peer_pids: Mapping[str, int | BaseException] | None = None,
        processes: Sequence[FakeProcess] = (),
        clock: FakeClock | None = None,
    ) -> None:
        # ``open_process`` and ``read_proc_stat`` answer from ``processes`` (by PID; a later
        # one replaces an earlier one, as a reused PID would), whose timed exits follow
        # ``clock``. ``pidfd_error`` makes ``open_process`` raise. ``handles`` lists every
        # handle opened and ``signals`` every (pid, signal) sent.
        self.clock = clock
        self.processes: dict[int, FakeProcess] = {}
        for process in processes:
            self.add_process(process)
        self.pidfd_error: OSError | None = None
        self.handles: list[FakeHandle] = []
        self.signals: list[tuple[int, int]] = []
        # ``platform`` answers ``system``; ``read_proc_stat`` answers from ``proc_stats``
        # (text, None for no such process, or an exception to raise), and an unscripted PID
        # fails the test.
        self.system = system
        self.proc_stats: dict[int, str | None | BaseException] = dict(proc_stats or {})
        self.proc_reads: list[int] = []
        # ``socket_peer_pid`` answers from ``peer_pids`` (socket path to the peer's PID, or an
        # exception to raise); an unscripted path fails the test.
        self.peer_pids: dict[str, int | BaseException] = dict(peer_pids or {})
        self.peer_checks: list[str] = []
        # ``proc_is_own_namespace`` answers ``own_namespace`` (or raises it).
        self.own_namespace: bool | OSError = True
        self.panes = None if panes is None else [dict(row) for row in panes]
        self.list_panes = list_panes
        self.responses: dict[tuple[str, ...], Response] = {
            (TMUX, "-V"): completed("tmux 3.5a\n"),
            **(responses or {}),
        }
        self.executables = {TMUX} if executables is None else set(executables)
        self.executable_checks: list[str] = []
        self.calls: list[list[str]] = []
        # The ``env`` argument of each call, in the order of ``calls``.
        self.envs: list[Mapping[str, str] | None] = []
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
        # FileInfo, and symlinks to their targets (a link's owner is the uid unless ``files``
        # has a ``symlink`` entry for it). By default a safe default server exists.
        self.files = dict(safe_default_server(uid) if files is None else files)
        self.links = dict(links or {})
        self.lstats: list[str] = []
        # ``make_dir`` calls as (path, mode); ``mkdir_error`` is raised by the next one, and
        # ``appears`` (paths with their lstat) is added to ``files`` just before it, as another
        # process creating them meanwhile would.
        self.mkdirs: list[tuple[str, int]] = []
        self.mkdir_error = mkdir_error
        self.appears: dict[str, FileInfo] = {}
        # run_group and spawn_watched answers, keyed by argv.
        self.group_responses: dict[tuple[str, ...], GroupResponse] = {}
        self.group_calls: list[GroupCall] = []
        # A stop (or any exception) raised after the call's ``record`` ran, as the real
        # gateway raises a stop signal that came while the run was being recorded; by argv.
        self.stops_after_record: dict[tuple[str, ...], BaseException] = {}

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

    def add_process(self, process: FakeProcess) -> None:
        process.clock = self.clock
        self.processes[process.pid] = process

    def open_process(self, pid: int) -> FakeHandle | None:
        if self.pidfd_error is not None:
            raise self.pidfd_error
        process = self.processes.get(pid)
        if process is None:
            raise AssertionError(f"unexpected process handle: {pid}")
        if process.done() and not process.zombie:
            return None
        handle = FakeHandle(process, self.signals)
        self.handles.append(handle)
        return handle

    def _respond(
        self, argv: list[str], env: Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[Any]:
        if any("kill-pane" in element for element in argv):
            for process in self.processes.values():
                if process.hup_on_kill:
                    process.exited = True
        self.calls.append(list(argv))
        self.envs.append(None if env is None else dict(env))
        try:
            response = self.responses[tuple(argv)]
        except KeyError:
            if is_list_panes(argv) and self.list_panes is not None:
                response = self.list_panes
            elif is_list_panes(argv) and self.panes is not None:
                sentinel = argv[-1].rsplit("\t", 1)[-1]
                response = completed(list_panes_stdout(self.panes, sentinel))
            else:
                response = None
        if response is None:
            raise AssertionError(f"unexpected gateway call: {argv!r}") from None
        if isinstance(response, BaseException):
            raise response
        return response

    def run_group(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        record: Callable[[GroupRun], None] | None = None,
    ) -> GroupRun:
        call = GroupCall(list(argv), stdin, dict(env), timeout, stdout_limit, stderr_limit, False)
        self.group_calls.append(call)
        response = self._group_response(argv)
        if isinstance(response, BaseException):
            raise response
        run = response if isinstance(response, GroupRun) else response(call)
        if record is not None:
            record(run)
        self._stop_after_record(argv)
        return run

    def spawn_watched(
        self,
        argv: list[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        timeout: float,
        record: Callable[[int], None] | None = None,
    ) -> int:
        self.group_calls.append(GroupCall(list(argv), stdin, dict(env), timeout, None, None, True))
        response = self._group_response(argv)
        if isinstance(response, BaseException):
            raise response
        if record is not None:
            record(4343)
        self._stop_after_record(argv)
        return 4343

    def _stop_after_record(self, argv: list[str]) -> None:
        stop = self.stops_after_record.get(tuple(argv))
        if stop is not None:
            raise stop

    def _group_response(self, argv: list[str]) -> GroupResponse:
        try:
            return self.group_responses[tuple(argv)]
        except KeyError:
            raise AssertionError(f"unexpected gateway group call: {argv!r}") from None

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

    def stdin_isatty(self) -> bool:
        return self.tty

    def user_name(self) -> str | None:
        return self.user

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

    def make_dir(self, path: str, mode: int) -> None:
        """Adds a directory owned by the uid with ``mode`` (no umask), as ``mkdir`` would."""
        self.mkdirs.append((path, mode))
        self.files.update(self.appears)
        self.appears = {}
        if self.mkdir_error is not None:
            error, self.mkdir_error = self.mkdir_error, None
            raise error
        if path in self.files or path in self.links:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), path)
        if os.path.dirname(path) not in self.files:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        self.files[path] = directory(mode, self.uid)

    def platform(self) -> str:
        return self.system

    def read_proc_stat(self, pid: int) -> str | None:
        self.proc_reads.append(pid)
        if pid in self.processes:
            return self.processes[pid].stat()
        if pid not in self.proc_stats:
            raise AssertionError(f"unexpected /proc read: {pid}")
        answer = self.proc_stats[pid]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def socket_peer_pid(self, path: str) -> int:
        self.peer_checks.append(path)
        if path not in self.peer_pids:
            raise AssertionError(f"unexpected socket peer check: {path}")
        answer = self.peer_pids[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def proc_is_own_namespace(self) -> bool:
        if isinstance(self.own_namespace, OSError):
            raise self.own_namespace
        return self.own_namespace

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
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleep(self, seconds: float) -> None:
        """Waits advance the clock at once; ``sleeps`` records them."""
        self.sleeps.append(seconds)
        self.advance(seconds)
