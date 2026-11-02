"""``procs``: a process's parent, start time and zombie state (interface.md "Liveness", "Open
order" step 7), from ``/proc`` (Linux and WSL) and ``ps`` (macOS), through the gateway."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from termctl import gateway as gateway_module
from termctl import procs
from termctl.gateway import SystemGateway

from .fakes import FakeClock, FakeGateway, FakeProcess, completed

# A /proc/<pid>/stat line: pid 4242, state S, parent 77, start time 823520334 (field 22).
TAIL = "0 -1 4194304 131 0 0 0 0 0 0 0 20 0 1 0 823520334 3436544 70 18446744073709551615 0"


def stat_line(pid: int = 4242, comm: str = "sleep", state: str = "S", ppid: int = 77) -> str:
    return f"{pid} ({comm}) {state} {ppid} {pid} {pid} {TAIL}\n"


needs_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc")


def linux(pid: int, answer: str | None | BaseException) -> FakeGateway:
    return FakeGateway(proc_stats={pid: answer})


# --- Linux and WSL: /proc/<pid>/stat ------------------------------------------------------------


def test_linux_reads_parent_start_time_and_state() -> None:
    gateway = linux(4242, stat_line())
    assert procs.read(gateway, 4242) == procs.Process(ppid=77, start="823520334", zombie=False)
    assert gateway.proc_reads == [4242]
    assert gateway.calls == []


@pytest.mark.parametrize(
    "comm", ["a) Z 1 2", "x y", ")", "((", ") R 1 ", "éè", "tab\there", "", "a\nb"]
)
def test_a_hostile_command_name_cannot_forge_the_fields(comm: str) -> None:
    # The name is in parentheses and may contain anything; fields start after the last ")".
    process = procs.read(linux(4242, stat_line(comm=comm)), 4242)
    assert process == procs.Process(ppid=77, start="823520334", zombie=False)


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_a_zombie_or_dead_process_is_reported(state: str) -> None:
    process = procs.read(linux(4242, stat_line(state=state)), 4242)
    assert process is not None and process.zombie


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "I", "W", "P"])
def test_other_states_are_not_zombies(state: str) -> None:
    process = procs.read(linux(4242, stat_line(state=state)), 4242)
    assert process is not None and not process.zombie


def test_a_missing_process_is_none() -> None:
    assert procs.read(linux(4242, None), 4242) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "4242",
        "4242 (sleep",
        "4242 (sleep) S",
        "4242 (sleep) S 77",
        "4242 (sleep) S x 4242 4242 " + TAIL,
        "4242 (sleep) S -1 4242 4242 " + TAIL,
        "4242 (sleep) S 77 4242 4242 " + TAIL.replace("823520334", "-5"),
        "4242 (sleep) S 77 4242 4242 " + TAIL.replace("823520334", "1" * 21),
        "4242 (sleep) S 77 4242 4242 " + TAIL.replace("823520334", "١٢"),
        "4242 (sleep) SS 77 4242 4242 " + TAIL,
        stat_line(pid=4243),
        "x" * 100000,
    ],
)
def test_malformed_stat_is_an_error(text: str) -> None:
    with pytest.raises(procs.ProcessError):
        procs.read(linux(4242, text), 4242)


def test_an_unreadable_stat_is_an_error() -> None:
    error = PermissionError(errno.EACCES, os.strerror(errno.EACCES), "/proc/4242/stat")
    with pytest.raises(procs.ProcessError, match="4242"):
        procs.read(linux(4242, error), 4242)


@pytest.mark.parametrize("pid", [0, -1, 10**10])
def test_pids_outside_the_grammar_are_refused(pid: int) -> None:
    with pytest.raises(ValueError):
        procs.read(FakeGateway(), pid)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_linux_reads_this_process_for_real() -> None:
    process = procs.read(SystemGateway(), os.getpid())
    assert process is not None
    assert process.ppid == os.getppid()
    assert not process.zombie
    assert process.start == Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_linux_sees_a_real_zombie_and_a_reaped_process() -> None:
    child = subprocess.Popen(["/bin/true"])
    gateway = SystemGateway()

    def zombie() -> bool:
        process = procs.read(gateway, child.pid)
        return process is not None and process.zombie

    deadline = time.monotonic() + 5
    while not zombie() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert zombie()
    child.wait()
    assert procs.read(gateway, child.pid) is None


# --- macOS: ps ----------------------------------------------------------------------------------

PS = ("/bin/ps", "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", "4242")


def mac(answer: subprocess.CompletedProcess[str] | BaseException) -> FakeGateway:
    return FakeGateway({PS: answer}, system="darwin")


def test_macos_reads_ps_in_the_c_locale_and_utc() -> None:
    gateway = mac(completed("   77 Ss   Fri Oct 30 23:25:20 2026\n"))
    process = procs.read(gateway, 4242)
    # 2026-10-30T23:25:20Z in seconds since the epoch.
    assert process == procs.Process(ppid=77, start="1793402720", zombie=False)
    assert gateway.calls == [list(PS)]
    assert gateway.envs == [{"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}]
    assert gateway.proc_reads == []


def test_macos_pads_single_digit_days() -> None:
    process = procs.read(mac(completed("1 S Sat Oct  3 01:02:03 2026\n")), 4242)
    assert process == procs.Process(ppid=1, start="1790989323", zombie=False)


@pytest.mark.parametrize("stat", ["Z", "Z+", "Zs"])
def test_macos_zombie(stat: str) -> None:
    process = procs.read(mac(completed(f"77 {stat} Fri Oct 30 23:25:20 2026\n")), 4242)
    assert process is not None and process.zombie


@pytest.mark.parametrize("result", [completed("", returncode=1), completed("\n", returncode=1)])
def test_macos_missing_process_is_none(result: subprocess.CompletedProcess[str]) -> None:
    assert procs.read(mac(result), 4242) is None


@pytest.mark.parametrize(
    "answer",
    [
        completed("77 S Fri Oct 30 23:25:20 2026\n", returncode=1),
        completed("77 S\n"),
        completed("x S Fri Oct 30 23:25:20 2026\n"),
        completed("77 S Fri Oct 30 25:25:20 2026\n"),
        completed("77 S Vendredi Oct 30 23:25:20 2026\n"),
        completed("77 S Fri Oct 30 23:25:20 2026\n78 S Fri Oct 30 23:25:20 2026\n"),
        completed(""),
        # ps failed rather than finding no such process (review round 1, R1-2).
        completed("", stderr="ps: cannot read the process table\n", returncode=1),
        # Only exit 1 with no output means no such process: ps killed by a signal, or failing
        # with another status, fails closed (review round 1 of #26, N).
        completed("", returncode=-9),
        completed("", returncode=2),
        # A start time before the epoch is outside the @termctl_pid_start grammar (R1-3).
        completed("77 S Wed Dec 31 23:59:59 1969\n"),
        subprocess.TimeoutExpired(list(PS), 10),
        FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/ps"),
    ],
)
def test_macos_failures_are_errors(
    answer: subprocess.CompletedProcess[str] | BaseException,
) -> None:
    with pytest.raises(procs.ProcessError):
        procs.read(mac(answer), 4242)


# --- close --force: verify ----------------------------------------------------------------------

SOCK = "/tmp/tmux-1000/default"
SERVER = 77
START = "123456"


def proving(*, peer: int | BaseException = SERVER, **process: object) -> FakeGateway:
    return FakeGateway(peer_pids={SOCK: peer}, processes=[FakeProcess(**process)])  # type: ignore[arg-type]


def test_verify_accepts_the_servers_live_child_with_the_recorded_start() -> None:
    gateway = proving()
    assert procs.verify(gateway, SOCK, 4242, START, SERVER) == procs.Verification(
        procs.Identity.VERIFIED
    )
    assert gateway.peer_checks == [SOCK] and gateway.proc_reads == [4242]


@pytest.mark.parametrize(
    ("gateway", "identity"),
    [
        # Another PID namespace: the server is not #{pid} here (0 when not visible).
        (proving(peer=0), procs.Identity.MISMATCH),
        (proving(peer=78), procs.Identity.MISMATCH),
        # PID reused, or a pane process whose parent is not the server.
        (proving(start="999"), procs.Identity.MISMATCH),
        (proving(ppid=1), procs.Identity.MISMATCH),
        # It has exited: a zombie counts as exited.
        (proving(zombie=True, exited=True), procs.Identity.GONE),
        (proving(exited=True), procs.Identity.GONE),
    ],
)
def test_verify_refuses_anything_but_the_recorded_process(
    gateway: FakeGateway, identity: procs.Identity
) -> None:
    assert procs.verify(gateway, SOCK, 4242, START, SERVER).identity is identity


def test_verify_checks_the_namespace_before_reading_the_process() -> None:
    gateway = proving(peer=0)
    procs.verify(gateway, SOCK, 4242, START, SERVER)
    assert gateway.proc_reads == []


@pytest.mark.parametrize(
    "gateway",
    [
        proving(peer=OSError(errno.EACCES, "Permission denied")),
        FakeGateway(peer_pids={SOCK: SERVER}, proc_stats={4242: OSError(errno.EIO, "I/O")}),
        FakeGateway(peer_pids={SOCK: SERVER}, proc_stats={4242: "garbage"}),
    ],
)
def test_verify_that_cannot_check_is_uncheckable_with_a_reason(gateway: FakeGateway) -> None:
    found = procs.verify(gateway, SOCK, 4242, START, SERVER)
    assert found.identity is procs.Identity.UNCHECKABLE and found.problem


@pytest.mark.parametrize(
    ("own", "problem"),
    [
        (False, "/proc belongs to another PID namespace than termctl"),
        (OSError(errno.ENOENT, "No such file or directory"), "cannot read /proc/self"),
    ],
)
def test_verify_with_a_proc_of_another_pid_namespace_is_uncheckable(
    own: bool | OSError, problem: str
) -> None:
    gateway = proving()
    gateway.own_namespace = own
    found = procs.verify(gateway, SOCK, 4242, START, SERVER)
    assert found.identity is procs.Identity.UNCHECKABLE
    assert found.problem is not None and problem in found.problem
    assert gateway.proc_reads == []


@needs_proc
def test_this_process_sees_its_own_pid_namespace_in_proc() -> None:
    assert SystemGateway().proc_is_own_namespace() is True
    assert procs.namespace_problem(SystemGateway()) is None


def test_verify_on_macos_skips_the_namespace_check() -> None:
    lstart = "Thu Jan  1 00:00:10 2026"
    gateway = FakeGateway(
        system="darwin",
        responses={
            (procs.PS, "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", "4242"): completed(
                f"77 S {lstart}\n"
            )
        },
    )
    found = procs.verify(gateway, SOCK, 4242, "1767225610", SERVER)
    assert found.identity is procs.Identity.VERIFIED
    assert gateway.peer_checks == []


# --- close --force: the signal sequence ---------------------------------------------------------


def sequence(
    grace: float = 10.0, **process: object
) -> tuple[procs.SignalOutcome, FakeProcess, FakeClock, FakeGateway]:
    clock = FakeClock()
    target = FakeProcess(**process)  # type: ignore[arg-type]
    gateway = FakeGateway(processes=[target], clock=clock)
    handle = gateway.open_process(4242)
    assert handle is not None
    started = clock.monotonic()
    outcome = procs.signal_sequence(gateway, clock, handle, 4242, START, grace)
    clock.elapsed = clock.monotonic() - started  # type: ignore[attr-defined]
    return outcome, target, clock, gateway


def test_f3_a_process_that_ignores_everything_gets_term_after_grace_then_kill_5s_later() -> None:
    outcome, target, clock, _ = sequence(grace=10.0)
    assert outcome == procs.SignalOutcome(["SIGTERM", "SIGKILL"], exited=False)
    assert target.signals == [signal.SIGTERM, signal.SIGKILL]
    # grace, then 5 s, then 1 s for the SIGKILL to take.
    assert clock.elapsed == pytest.approx(16.0)  # type: ignore[attr-defined]


def test_f3_term_is_sent_only_after_the_grace_period() -> None:
    clock = FakeClock()
    target = FakeProcess(exits_on=frozenset({signal.SIGTERM}))
    gateway = FakeGateway(processes=[target], clock=clock)
    handle = gateway.open_process(4242)
    assert handle is not None
    sent_at: list[float] = []
    real = handle.send_signal

    def send(signum: int) -> bool:
        sent_at.append(clock.monotonic())
        return real(signum)

    handle.send_signal = send  # type: ignore[method-assign]
    start = clock.monotonic()
    outcome = procs.signal_sequence(gateway, clock, handle, 4242, START, 2.5)
    assert outcome == procs.SignalOutcome(["SIGTERM"], exited=True)
    assert sent_at == [pytest.approx(start + 2.5)]


def test_a_process_that_exits_on_sigterm_gets_no_sigkill() -> None:
    outcome, target, _, _ = sequence(exits_on=frozenset({signal.SIGTERM}))
    assert outcome == procs.SignalOutcome(["SIGTERM"], exited=True)
    assert target.signals == [signal.SIGTERM]


def test_a_process_that_exits_within_the_grace_gets_no_signal() -> None:
    outcome, target, clock, _ = sequence(grace=10.0, exit_at=1003.0)
    assert outcome == procs.SignalOutcome([], exited=True)
    assert target.signals == []
    # Stopped as soon as it exited, not at the end of the grace period.
    assert clock.elapsed < 3.2  # type: ignore[attr-defined]


def test_zero_grace_sends_sigterm_at_once() -> None:
    outcome, _, clock, _ = sequence(grace=0.0, exits_on=frozenset({signal.SIGTERM}))
    assert outcome.signals_sent == ["SIGTERM"]
    assert clock.elapsed == 0  # type: ignore[attr-defined]


def test_a_zombie_counts_as_exited() -> None:
    outcome, target, _, _ = sequence(zombie=True, exited=True)
    assert outcome == procs.SignalOutcome([], exited=True)
    assert target.signals == []


def test_a_start_time_mismatch_before_any_signal_is_pid_mismatch() -> None:
    outcome, target, _, _ = sequence(start="999")
    assert outcome == procs.SignalOutcome([], exited=None, pid_mismatch=True)
    assert target.signals == []


def test_a_reused_pid_after_sigterm_is_never_signaled() -> None:
    clock = FakeClock()
    first = FakeProcess(exits_on=frozenset({signal.SIGTERM}))
    gateway = FakeGateway(processes=[first], clock=clock)
    handle = gateway.open_process(4242)
    assert handle is not None
    real = handle.send_signal

    def send(signum: int) -> bool:
        sent = real(signum)
        # The process exits and its PID is reused at once by an unrelated process.
        gateway.add_process(FakeProcess(start="999"))
        return sent

    handle.send_signal = send  # type: ignore[method-assign]
    outcome = procs.signal_sequence(gateway, clock, handle, 4242, START, 0.0)
    assert outcome == procs.SignalOutcome(["SIGTERM"], exited=True)
    assert gateway.processes[4242].signals == []


def test_an_unreadable_process_stops_the_sequence_with_a_warning() -> None:
    clock = FakeClock()
    gateway = FakeGateway(processes=[FakeProcess()], clock=clock)
    handle = gateway.open_process(4242)
    assert handle is not None
    gateway.processes.clear()
    gateway.proc_stats[4242] = OSError(errno.EIO, "I/O error")
    outcome = procs.signal_sequence(gateway, clock, handle, 4242, START, 0.0)
    assert outcome.signals_sent == [] and outcome.exited is False
    assert outcome.warnings and "not signaling" in outcome.warnings[0]


def test_a_signal_refused_by_the_kernel_stops_with_a_warning() -> None:
    clock = FakeClock()
    gateway = FakeGateway(processes=[FakeProcess()], clock=clock)
    handle = gateway.open_process(4242)
    assert handle is not None
    handle.error = PermissionError(errno.EPERM, "Operation not permitted")
    outcome = procs.signal_sequence(gateway, clock, handle, 4242, START, 0.0)
    assert outcome.signals_sent == [] and outcome.exited is False
    assert "cannot send SIGTERM" in outcome.warnings[0]


# --- the real pidfd handle (Linux) --------------------------------------------------------------

needs_pidfd = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux pidfd")


@needs_pidfd
@pytest.mark.parametrize("pid", [0, -1, 2**31, 2**32 + 1])
def test_pidfd_open_refuses_a_pid_no_process_can_have(
    pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before any system call: the raw fallback would truncate it to a C int.
    def no_syscall(*args: object) -> int:
        raise AssertionError("a system call was made")

    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(gateway_module, "_syscall", no_syscall)
    with pytest.raises(ProcessLookupError):
        gateway_module._pidfd_open(pid)
    assert SystemGateway().open_process(pid) is None


@pytest.mark.parametrize(
    ("system", "machine", "pointer"),
    [
        ("linux", "mips64", 8),
        ("linux", "mips", 4),
        ("linux", "ia64", 8),
        ("linux", "alpha", 8),
        ("linux", "x86_64", 4),  # x32
        ("freebsd14", "x86_64", 8),
    ],
)
def test_raw_pidfd_calls_are_refused_where_their_numbers_are_unknown(
    system: str, machine: str, pointer: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module.sys, "platform", system)
    monkeypatch.setattr(gateway_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(gateway_module.ctypes, "sizeof", lambda _: pointer)
    with pytest.raises(OSError) as info:
        gateway_module._syscall(gateway_module._SYS_PIDFD_OPEN, None)
    assert info.value.errno == errno.ENOSYS


@pytest.mark.parametrize(
    "machine", ["x86_64", "i686", "aarch64", "armv7l", "riscv64", "ppc64le", "ppc64", "s390x"]
)
def test_raw_pidfd_call_numbers_are_known_on_the_unified_architectures(
    machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module.sys, "platform", "linux")
    monkeypatch.setattr(gateway_module.platform, "machine", lambda: machine)
    monkeypatch.setattr(gateway_module.ctypes, "sizeof", lambda _: 8)
    assert gateway_module._syscall_numbers_known()


@needs_pidfd
def test_without_known_pidfd_calls_no_handle_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    # ENOSYS from the raw fallback is a handle that cannot be opened: uncheckable.
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.setattr(gateway_module.platform, "machine", lambda: "mips64")
    with pytest.raises(OSError) as info:
        SystemGateway().open_process(os.getpid())
    assert info.value.errno == errno.ENOSYS


@needs_pidfd
@pytest.mark.parametrize("raw_syscalls", [False, True])
def test_pidfd_handle_signals_only_its_own_process(
    raw_syscalls: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw_syscalls:  # as on a Python build whose os module lacks the pidfd functions
        monkeypatch.delattr(os, "pidfd_open", raising=False)
        monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    child = subprocess.Popen(["/bin/sleep", "60"])
    try:
        handle = SystemGateway().open_process(child.pid)
        assert handle is not None
        assert not handle.exited()
        assert handle.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == -signal.SIGTERM
        # Reaped: the handle still names the old process and reaches nobody.
        assert handle.exited()
        assert handle.send_signal(signal.SIGTERM) is False
        handle.close()
        handle.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@needs_pidfd
def test_pidfd_handle_sees_a_zombie_as_exited() -> None:
    child = subprocess.Popen(["/bin/true"])
    try:
        handle = SystemGateway().open_process(child.pid)
        assert handle is not None
        deadline = time.monotonic() + 5
        while not handle.exited() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handle.exited()  # exited but not yet reaped: a zombie
        handle.close()
    finally:
        child.wait()


@needs_pidfd
def test_pidfd_of_a_missing_process_is_none() -> None:
    child = subprocess.Popen(["/bin/true"])
    child.wait()
    assert SystemGateway().open_process(child.pid) is None
