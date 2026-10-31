"""``procs``: a process's parent, start time and zombie state (interface.md "Liveness", "Open
order" step 7), from ``/proc`` (Linux and WSL) and ``ps`` (macOS), through the gateway."""

from __future__ import annotations

import errno
import os
import subprocess
import time
from pathlib import Path

import pytest

from termctl import procs
from termctl.gateway import SystemGateway

from .fakes import FakeGateway, completed

# A /proc/<pid>/stat line: pid 4242, state S, parent 77, start time 823520334 (field 22).
TAIL = "0 -1 4194304 131 0 0 0 0 0 0 0 20 0 1 0 823520334 3436544 70 18446744073709551615 0"


def stat_line(pid: int = 4242, comm: str = "sleep", state: str = "S", ppid: int = 77) -> str:
    return f"{pid} ({comm}) {state} {ppid} {pid} {pid} {TAIL}\n"


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
