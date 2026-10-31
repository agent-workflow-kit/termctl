"""The macOS-only code paths, run on Linux by injection (#26; design.md "macOS checklist").

CI has no macOS runner, so each macOS path is exercised here with what it reads replaced:

- the kqueue ``NOTE_EXIT`` watch (``gateway._KqueueExit``) by ``KqueueEmulation``, the
  ``select`` kqueue API backed by a Linux pidfd, which, like a kqueue registration, stays tied
  to the process that had the PID when it was made. It runs the hook leader's exit detection
  (``_LeaderExit``, macOS before Python 3.13) and the ``close --force`` handle (``_PidHandle``)
  against real child processes;
- ``ps`` output (``procs._read_ps``) by scripted text;
- the platform (``Gateway.platform``) by ``FakeGateway(system="darwin")`` or a
  ``SystemGateway`` subclass.

What the emulation cannot show (the real kernel's answers) is on the manual checklist.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from termctl import gateway as gateway_module
from termctl import procs
from termctl.gateway import SystemGateway

from .fakes import FakeGateway, FakeProcess, completed
from .test_cli_close import (
    HUMAN,
    ID,
    NOMATCH,
    QUERY,
    changing,
    close,
    dry_output,
    gateway_for,
    hook,
)
from .test_gateway import group, group_is_gone, is_gone, wait_gone

# Through gateway._pidfd_open, which makes the system call itself where os lacks it.
needs_pidfd = pytest.mark.skipif(
    not Path("/proc/self/stat").exists(), reason="the kqueue emulation needs Linux pidfd"
)

# select's constants on macOS (sys/event.h).
KQ_FILTER_PROC = -5
KQ_EV_ADD = 0x0001
KQ_EV_ONESHOT = 0x0010
KQ_NOTE_EXIT = 0x80000000


class _EmulatedKqueue:
    def __init__(self, api: KqueueEmulation) -> None:
        self.api = api
        self.fds: dict[int, int] = {}
        self.closed = False

    def control(self, changes: list[Any] | None, max_events: int, timeout: float) -> list[Any]:
        assert not self.closed
        assert timeout == 0, "termctl only polls the kqueue"
        if changes:
            assert max_events == 0
            for ident, filt, flags, fflags in changes:
                assert filt == KQ_FILTER_PROC
                assert flags == KQ_EV_ADD | KQ_EV_ONESHOT
                assert fflags == KQ_NOTE_EXIT
                if self.api.refuse is not None:
                    raise self.api.refuse
                self.api.registered.append(ident)
                self.fds[ident] = gateway_module._pidfd_open(ident)  # ProcessLookupError if none
            return []
        assert changes is None and max_events == 1
        for ident, fd in list(self.fds.items()):
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            if poller.poll(0):  # the process has exited (a zombie included)
                del self.fds[ident]  # EV_ONESHOT: delivered once
                os.close(fd)
                return [(ident, KQ_FILTER_PROC, 0, KQ_NOTE_EXIT)]
        return []

    def close(self) -> None:
        self.closed = True
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()


class KqueueEmulation:
    """``select``'s kqueue API for ``EVFILT_PROC``/``NOTE_EXIT`` only, backed by a pidfd.
    ``refuse`` makes every registration raise it; ``registered`` lists each PID watched."""

    KQ_FILTER_PROC = KQ_FILTER_PROC
    KQ_EV_ADD = KQ_EV_ADD
    KQ_EV_ONESHOT = KQ_EV_ONESHOT
    KQ_NOTE_EXIT = KQ_NOTE_EXIT

    def __init__(self) -> None:
        self.refuse: OSError | None = None
        self.registered: list[int] = []
        self.queues: list[_EmulatedKqueue] = []

    def kqueue(self) -> _EmulatedKqueue:
        queue = _EmulatedKqueue(self)
        self.queues.append(queue)
        return queue

    @staticmethod
    def kevent(ident: int, *, filter: int, flags: int, fflags: int) -> tuple[int, int, int, int]:  # noqa: A002
        return (ident, filter, flags, fflags)


@pytest.fixture
def kqueue(monkeypatch: pytest.MonkeyPatch) -> Iterator[KqueueEmulation]:
    """The kqueue emulation in place of ``select``'s, and no ``os.waitid`` (as on macOS
    before Python 3.13)."""
    api = KqueueEmulation()
    monkeypatch.setattr(gateway_module, "_kqueue_api", lambda: api)
    monkeypatch.setattr(gateway_module, "_HAS_WAITID", False)
    yield api
    assert all(queue.closed for queue in api.queues), "a kqueue was left open"


class DarwinGateway(SystemGateway):
    def platform(self) -> str:
        return "darwin"


def child(*argv: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(list(argv or ("/bin/sleep", "60")))


def stop(proc: subprocess.Popen[bytes]) -> None:
    """Kill and reap a child this test started (by its own handle, never by pattern)."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


# --- close --force on macOS: the kqueue process handle ------------------------------------------


@needs_pidfd
def test_macos_handle_watches_the_pid_and_signals_its_live_process(
    kqueue: KqueueEmulation,
) -> None:
    proc = child()
    try:
        handle = DarwinGateway().open_process(proc.pid)
        assert isinstance(handle, gateway_module._PidHandle)
        assert kqueue.registered == [proc.pid]
        assert not handle.exited()
        assert handle.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == -signal.SIGTERM
        assert handle.exited()
        handle.close()
        handle.close()
    finally:
        stop(proc)


@needs_pidfd
def test_macos_handle_never_kills_once_its_process_has_exited(
    kqueue: KqueueEmulation, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The race of #23: the recorded process exits and its PID could be reused before the
    # kill. Once the watch has fired, no kill is made at all, whoever holds the PID now.
    proc = child("/bin/true")
    handle = DarwinGateway().open_process(proc.pid)
    assert handle is not None
    proc.wait()
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(gateway_module.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    try:
        assert handle.exited()
        assert handle.send_signal(signal.SIGTERM) is False
        assert handle.send_signal(signal.SIGKILL) is False
        assert kills == []
    finally:
        handle.close()


@needs_pidfd
def test_macos_handle_sees_a_zombie_as_exited(kqueue: KqueueEmulation) -> None:
    proc = child("/bin/true")
    try:
        handle = DarwinGateway().open_process(proc.pid)
        assert handle is not None
        deadline = time.monotonic() + 5
        while not handle.exited() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handle.exited()  # exited, not yet reaped
        assert handle.send_signal(signal.SIGTERM) is False
        handle.close()
    finally:
        proc.wait()


@needs_pidfd
@pytest.mark.parametrize("pid", ["reaped", 0, -1, 2**31])
def test_macos_handle_of_no_process_is_none(kqueue: KqueueEmulation, pid: int | str) -> None:
    if pid == "reaped":
        proc = child("/bin/true")
        proc.wait()
        pid = proc.pid
    assert isinstance(pid, int)
    assert DarwinGateway().open_process(pid) is None


@needs_pidfd
def test_macos_handle_that_cannot_be_registered_raises(kqueue: KqueueEmulation) -> None:
    # close_cmd._open turns this into a warning and pid_mismatch: nothing is signaled.
    kqueue.refuse = PermissionError(errno.EPERM, "Operation not permitted")
    proc = child()
    try:
        with pytest.raises(PermissionError):
            DarwinGateway().open_process(proc.pid)
    finally:
        stop(proc)


def test_macos_handle_without_kqueue_raises_enosys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_module, "_kqueue_api", lambda: None)
    with pytest.raises(OSError) as info:
        DarwinGateway().open_process(os.getpid())
    assert info.value.errno == errno.ENOSYS


@needs_pidfd
def test_a_closed_macos_handle_is_refused(kqueue: KqueueEmulation) -> None:
    proc = child()
    try:
        handle = DarwinGateway().open_process(proc.pid)
        assert handle is not None
        handle.close()
        with pytest.raises(ValueError):
            handle.exited()
        with pytest.raises(ValueError):
            handle.send_signal(signal.SIGTERM)
    finally:
        stop(proc)


@needs_pidfd
def test_signal_sequence_with_the_macos_handle_and_ps(kqueue: KqueueEmulation) -> None:
    # procs.signal_sequence as close --force runs it on macOS: ps (scripted) gives the start
    # time, the real handle signals a real child, which exits on SIGTERM.
    proc = child()
    try:
        ps = ("/bin/ps", "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", str(proc.pid))
        gateway = FakeGateway({ps: completed("77 S Fri Oct 30 23:25:20 2026\n")}, system="darwin")
        handle = DarwinGateway().open_process(proc.pid)
        assert handle is not None
        try:
            outcome = procs.signal_sequence(
                gateway, gateway_module.SystemClock(), handle, proc.pid, "1793402720", 0.0
            )
        finally:
            handle.close()
        assert (outcome.signals_sent, outcome.exited, outcome.warnings) == (["SIGTERM"], True, [])
        assert proc.wait(timeout=5) == -signal.SIGTERM
    finally:
        stop(proc)


# --- hooks on macOS before Python 3.13: the kqueue leader-exit watch ----------------------------


@needs_pidfd
def test_hook_exit_status_through_the_kqueue_watch(kqueue: KqueueEmulation) -> None:
    run = group("exit 3")
    assert run.returncode == 3
    assert len(kqueue.registered) == 1


@needs_pidfd
def test_hook_whose_grandchild_holds_stdout_ends_one_second_after_the_shell(
    kqueue: KqueueEmulation,
) -> None:
    start = time.monotonic()
    run = group("sleep 6 & echo $!; exit 4", timeout=5)
    elapsed = time.monotonic() - start
    assert run.returncode == 4
    assert elapsed < 2.5  # LEADER_EXIT_GRACE_S after the exit, not the 5 s timeout
    grandchild = int(run.stdout)
    assert not is_gone(grandchild)  # left running, as on Linux
    os.kill(grandchild, signal.SIGKILL)  # the PID this test's hook just printed
    assert wait_gone(grandchild)


@needs_pidfd
def test_hook_timeout_kills_the_group_through_the_kqueue_watch(
    kqueue: KqueueEmulation, tmp_path: Path
) -> None:
    marker = tmp_path / "survived"
    start = time.monotonic()
    run = group(f"(sleep 5; touch '{marker}') & echo $!; sleep 10", timeout=0.5)
    assert time.monotonic() - start < 3
    assert run.timed_out
    assert wait_gone(int(run.stdout))
    assert group_is_gone(run.pgid)
    assert not marker.exists()


def test_hook_leader_watch_without_waitid_or_kqueue_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "_kqueue_api", lambda: None)
    monkeypatch.setattr(gateway_module, "_HAS_WAITID", False)
    leader = gateway_module._LeaderExit(os.getpid())
    assert not leader.available and not leader.exited()
    leader.close()
    assert group("exit 5").returncode == 5  # the exit is seen when the streams close


@needs_pidfd
def test_hook_leader_watch_that_cannot_register_is_unavailable(kqueue: KqueueEmulation) -> None:
    kqueue.refuse = OSError(errno.EMFILE, "Too many open files")
    leader = gateway_module._LeaderExit(os.getpid())
    assert not leader.available
    leader.close()


# --- SO_PEERCRED: Linux only, fail closed elsewhere -----------------------------------------------


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        yield path


@pytest.mark.parametrize("system", ["darwin", "freebsd14", "openbsd7"])
def test_socket_peer_pid_is_refused_off_linux(
    system: str, listener: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenBSD's SO_PEERCRED is (uid, gid, pid): read as Linux's it would give the uid.
    monkeypatch.setattr(gateway_module.sys, "platform", system)
    with pytest.raises(OSError) as info:
        SystemGateway().socket_peer_pid(listener)
    assert info.value.errno == errno.ENOPROTOOPT


def test_socket_peer_pid_without_so_peercred_raises_oserror_not_attributeerror(
    listener: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    with pytest.raises(OSError) as info:
        SystemGateway().socket_peer_pid(listener)
    assert info.value.errno == errno.ENOPROTOOPT


def test_close_probe_off_linux_and_macos_is_uncheckable_not_a_crash(
    listener: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A system that is neither Linux nor macOS takes the Linux checks; SO_PEERCRED cannot be
    # read there, so the recorded PID is uncheckable (pid_mismatch, a warning), never read.
    monkeypatch.setattr(gateway_module.sys, "platform", "freebsd14")
    verification = procs.verify(SystemGateway(), listener, os.getpid(), "1", os.getppid())
    assert verification.identity is procs.Identity.UNCHECKABLE
    assert verification.problem is not None
    assert "SO_PEERCRED is not available on freebsd14" in verification.problem


# --- ps parsing (macOS process reads) -------------------------------------------------------------

PS = ("/bin/ps", "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", "4242")


def ps_read(stdout: str) -> procs.Process | None:
    return procs.read(FakeGateway({PS: completed(stdout)}, system="darwin"), 4242)


@pytest.mark.parametrize(
    ("stdout", "start"),
    [
        # strftime("%c") in the C locale pads the day with a blank.
        ("  77 Ss   Fri Oct  2 09:05:01 2026\n", "1790931901"),
        ("77 S+ Fri Oct 02 09:05:01 2026\n", "1790931901"),
        ("77\tS\tFri Oct 30 23:25:20 2026", "1793402720"),
        ("77 S Thu Feb 29 12:00:00 2024\n", "1709208000"),
        ("77 S Fri Jan  2 00:00:00 1970\n", "86400"),
    ],
)
def test_macos_lstart_forms_ps_prints(stdout: str, start: str) -> None:
    assert ps_read(stdout) == procs.Process(ppid=77, start=start, zombie=False)


@pytest.mark.parametrize(
    "lstart",
    [
        "Thu Oct 30 23:25:20 2026",  # Oct 30 2026 is a Friday
        "Sat Feb 29 12:00:00 2026",  # no Feb 29 in 2026
        "Fri Oct 31 24:00:00 2026",
        "Fri Oct 30 23:60:00 2026",
        "Fri Oct 30 23:25:60 2026",
        "Fri Oct 0 23:25:20 2026",
        "fri oct 30 23:25:20 2026",
        "Fr Okt 30 23:25:20 2026",  # a German LC_TIME that LC_ALL=C did not override
        "ven. oct. 30 23:25:20 2026",
        "Fri 30 Oct 23:25:20 2026",
        "Fri Oct 30 23:25:20 26",
        "Fri Oct 30 23:25 2026",
        "Fri Oct 30 23:25:20 2026 UTC",
        "Fri Oct 30 23:25:20.5 2026",
        "Fri Oct ３０ 23:25:20 2026",  # fullwidth digits
        "Fri Oct 30 23:25:20 ２０２６",
    ],
)
def test_macos_lstart_that_is_not_a_c_locale_time_is_an_error(lstart: str) -> None:
    with pytest.raises(procs.ProcessError):
        ps_read(f"77 S {lstart}\n")


def test_macos_lstart_parse_ignores_termctls_own_lc_time() -> None:
    import locale

    previous = locale.setlocale(locale.LC_TIME)
    for name in ("de_DE.UTF-8", "de_DE.utf8", "fr_FR.UTF-8", "fr_FR.utf8"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no German or French locale installed")
    try:
        assert ps_read("77 S Fri Oct 30 23:25:20 2026\n") == procs.Process(77, "1793402720", False)
    finally:
        locale.setlocale(locale.LC_TIME, previous)


# --- close on macOS through main: ps reads, no SO_PEERCRED, no /proc ------------------------------


def darwin(**kwargs: Any) -> tuple[FakeGateway, Any]:
    gateway, clock = gateway_for(**kwargs)
    gateway.system = "darwin"
    # Neither SO_PEERCRED nor /proc may be consulted on macOS.
    gateway.peer_pids = {}
    gateway.own_namespace = OSError(errno.ENOENT, "No such file or directory")
    return gateway, clock


def ps_calls(gateway: FakeGateway) -> list[list[str]]:
    return [call for call in gateway.calls if call[0] == "/bin/ps"]


def test_macos_forced_close_proves_the_process_with_ps_and_signals_it() -> None:
    gateway, clock = darwin(process=FakeProcess())
    result = close([ID, "--force", *HUMAN], gateway, clock)
    data = result.output()
    assert (data["signals_sent"], data["exited"], data["pid_mismatch"]) == (
        ["SIGTERM", "SIGKILL"],
        False,
        False,
    )
    assert gateway.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert gateway.peer_checks == [] and gateway.proc_reads == []
    assert ps_calls(gateway) and all(call[-1] == "4242" for call in ps_calls(gateway))
    ps_envs = [
        env for call, env in zip(gateway.calls, gateway.envs, strict=True) if call[0] == "/bin/ps"
    ]
    assert all(env == {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"} for env in ps_envs)


@pytest.mark.parametrize(
    "process", [FakeProcess(start="123457"), FakeProcess(ppid=1)], ids=["start", "parent"]
)
def test_macos_forced_close_of_another_process_is_a_mismatch(process: FakeProcess) -> None:
    gateway, clock = darwin(process=process)
    data = close([ID, "--force", *HUMAN], gateway, clock).output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (True, None, [])
    assert gateway.signals == []


def test_macos_forced_close_with_unreadable_ps_warns_and_signals_nothing() -> None:
    gateway, clock = darwin()
    gateway.responses[PS] = completed("77 S Fr Okt 30 23:25:20 2026\n")
    result = close([ID, "--force", *HUMAN], gateway, clock)
    data = result.output()
    assert (data["pid_mismatch"], data["signals_sent"]) == (True, [])
    assert "unexpected start time from /bin/ps for process 4242" in result.stderr
    assert gateway.signals == []


def test_macos_gone_at_the_probe_with_a_zombie_is_exited_without_proc() -> None:
    gateway, clock = darwin(responses={QUERY: NOMATCH})
    hook(gateway, "gate", changing(gateway, []))  # gone by the re-check too
    process = gateway.processes[4242]
    process.zombie = process.exited = True
    data = close([ID, "--force", *HUMAN, "--hook", "pre-close=gate"], gateway, clock).output()
    assert (data["pid_mismatch"], data["exited"], data["signals_sent"]) == (False, True, [])
    assert gateway.proc_reads == [] and gateway.peer_checks == []


def test_macos_dry_run_of_a_pane_gone_at_the_probe_skips_the_proc_check() -> None:
    gateway, clock = darwin(responses={QUERY: NOMATCH})
    gateway.processes[4242].exited = True
    result = close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock)
    data = dry_output(result)
    assert (data["pid_mismatch"], data["signals_possible"]) == (False, [])
    assert "warning" not in result.stderr
    assert ps_calls(gateway) and gateway.proc_reads == []


def test_macos_dry_run_of_a_running_pane_reports_the_signals() -> None:
    gateway, clock = darwin()
    data = dry_output(close([ID, "--force", *HUMAN, "--dry-run"], gateway, clock))
    assert (data["pid_mismatch"], data["signals_possible"]) == (False, ["SIGTERM", "SIGKILL"])
    assert gateway.peer_checks == []
