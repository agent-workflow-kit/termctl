from __future__ import annotations

import os
import pwd
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from termctl import gateway as gateway_module
from termctl import hookwatch
from termctl.gateway import (
    Clock,
    Gateway,
    GroupNotStopped,
    GroupRun,
    SystemClock,
    SystemGateway,
)

from .fakes import FakeClock, FakeGateway
from .procs import group_is_gone, is_gone, read_when_ready, reap, wait_gone, wait_until


def test_system_gateway_runs_argv_without_a_shell() -> None:
    result = SystemGateway().run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME;"])
    assert (result.returncode, result.stdout) == (0, "$HOME;\n")


def test_system_gateway_passes_stdin_and_env() -> None:
    script = "import os, sys; print(sys.stdin.read() + os.environ['X'])"
    result = SystemGateway().run([sys.executable, "-c", script], stdin="a", env={"X": "b"})
    assert result.stdout == "ab\n"


def test_system_clock_is_timezone_aware_and_monotonic() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is UTC
    first = clock.monotonic()
    assert clock.monotonic() >= first


def test_fakes_satisfy_the_protocols() -> None:
    gateway: Gateway = FakeGateway()
    clock: Clock = FakeClock()
    assert gateway is not None and clock is not None


def test_fake_clock_advances() -> None:
    clock = FakeClock()
    start, mono = clock.now(), clock.monotonic()
    clock.advance(2.5)
    assert (clock.now() - start).total_seconds() == 2.5
    assert clock.monotonic() - mono == 2.5


def test_system_gateway_current_dir_and_is_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    gateway = SystemGateway()
    assert gateway.current_dir() == os.getcwd()
    assert gateway.is_dir(str(tmp_path))
    (tmp_path / "file").write_text("x")
    assert not gateway.is_dir(str(tmp_path / "file"))
    assert not gateway.is_dir(str(tmp_path / "missing"))


def test_system_gateway_is_dir_follows_symlinks(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to(tmp_path)
    assert SystemGateway().is_dir(str(tmp_path / "link"))


def test_system_gateway_stdin_isatty_asks_about_fd_0(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    def isatty(fd: int) -> bool:
        asked.append(fd)
        return True

    monkeypatch.setattr(os, "isatty", isatty)
    assert SystemGateway().stdin_isatty() is True
    assert asked == [0]


def test_system_gateway_user_name_is_the_real_uid_user(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[int] = []
    real = pwd.getpwuid(os.getuid())

    def getpwuid(uid: int) -> pwd.struct_passwd:
        looked_up.append(uid)
        return pwd.struct_passwd(("alice", *real[1:]))

    monkeypatch.setattr(os, "getuid", lambda: 4242)
    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    assert SystemGateway().user_name() == "alice"
    assert looked_up == [4242]


def test_system_gateway_user_name_none_without_passwd_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(uid: int) -> pwd.struct_passwd:
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    assert SystemGateway().user_name() is None


# --- run_group ----------------------------------------------------------------------------------

SH = "/bin/sh"


def base_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def group(
    script: str,
    *,
    stdin: bytes = b"",
    env: dict[str, str] | None = None,
    timeout: float = 10,
    stdout_limit: int = 1024,
    stderr_limit: int = 1024,
) -> GroupRun:
    return SystemGateway().run_group(
        [SH, "-c", script],
        stdin=stdin,
        env=base_env() if env is None else env,
        timeout=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )


def test_run_group_passes_stdin_and_captures_both_streams() -> None:
    run = group("cat; echo err >&2; exit 3", stdin=b'{"a": 1}')
    assert run.returncode == 3
    assert (run.stdout, run.stderr) == (b'{"a": 1}', b"err\n")
    assert not run.timed_out
    assert not (run.stdout_truncated or run.stderr_truncated)


def test_run_group_env_replaces_the_environment() -> None:
    run = group('printf "%s|%s" "$X" "${HOME-unset}"', env={"X": "y"})
    assert run.stdout == b"y|unset"


def test_run_group_runs_in_its_own_process_group() -> None:
    run = group("ps -o pgid= -p $$")
    assert int(run.stdout) == run.pgid
    assert run.pgid != os.getpgrp()


def test_run_group_keeps_at_most_the_limit_and_drains_the_rest() -> None:
    # 1 MiB on each stream: far more than a pipe holds, so the child would block (and time
    # out) if the rest were not drained.
    script = "head -c 1048576 /dev/zero; head -c 1048576 /dev/zero >&2; exit 0"
    run = group(script, stdout_limit=10, stderr_limit=5, timeout=20)
    assert run.returncode == 0
    assert (run.stdout, run.stderr) == (b"\0" * 10, b"\0" * 5)
    assert run.stdout_truncated and run.stderr_truncated


def test_run_group_survives_a_child_that_never_reads_stdin() -> None:
    run = group("exit 0", stdin=b"x" * (4 * 1024 * 1024))
    assert run.returncode == 0


def test_run_group_writes_a_large_payload_to_a_reader() -> None:
    payload = b"y" * (1024 * 1024)
    run = group("wc -c", stdin=payload)
    assert int(run.stdout) == len(payload)


def test_run_group_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    # The background child keeps running after sh is killed unless the group is signaled.
    script = f"(sleep 5; touch '{marker}') & echo $!; echo partial >&2; sleep 10"
    start = time.monotonic()
    run = group(script, timeout=0.5)
    assert time.monotonic() - start < 3
    assert run.timed_out and run.returncode is None
    assert run.stderr == b"partial\n"
    assert wait_gone(int(run.stdout))
    assert group_is_gone(run.pgid)
    assert not marker.exists()


def test_run_group_timeout_kills_a_leader_that_left_its_group() -> None:
    # The leader moves itself into this test's process group, out of reach of the group kill.
    script = (
        f"exec '{sys.executable}' -c 'import os, time; "
        "os.setpgid(0, os.getpgid(os.getppid())); print(os.getpid(), flush=True); time.sleep(10)'"
    )
    start = time.monotonic()
    run = group(script, timeout=1)
    assert time.monotonic() - start < 4
    assert run.timed_out
    assert wait_gone(int(run.stdout))


@pytest.mark.parametrize(
    ("leftover", "status"),
    [
        ("sleep 6", 0),
        ("sleep 6", 3),
        (f"'{sys.executable}' -c 'import os, time; os.setsid(); time.sleep(6)'", 0),
    ],
)
def test_run_group_ends_soon_after_the_leader_exits_leaving_the_pipes_open(
    leftover: str, status: int
) -> None:
    # sh exits at once, but its background child keeps stdout and stderr open. The child is
    # not killed (it ends by itself).
    start = time.monotonic()
    run = group(f"{leftover} & echo $!; exit {status}", timeout=3)
    elapsed = time.monotonic() - start
    assert run.returncode == status
    assert elapsed < 2.5
    assert not is_gone(int(run.stdout))


def test_run_group_reports_a_signal_as_a_negative_returncode() -> None:
    run = group("kill -TERM $$")
    assert run.returncode == -signal.SIGTERM


def test_run_group_reports_the_exit_status_with_sigchld_ignored() -> None:
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        run = group("exit 1")
    finally:
        signal.signal(signal.SIGCHLD, previous)
    assert run.returncode == 1


def test_run_group_raises_oserror_when_it_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SystemGateway().run_group(
            [str(tmp_path / "missing")],
            stdin=b"",
            env={},
            timeout=1,
            stdout_limit=1,
            stderr_limit=1,
        )


def test_run_group_kills_the_group_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(self: selectors.BaseSelector, timeout: float | None = None) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(selectors.DefaultSelector, "select", interrupt)
    seen: list[int] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        seen.append(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(KeyboardInterrupt):
        group("sleep 10", timeout=10)
    assert group_is_gone(seen[0])


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_run_group_reports_a_group_it_cannot_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(pid: int, signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    monkeypatch.setattr(os, "kill", refuse)
    monkeypatch.setattr(gateway_module, "STOP_WAIT_S", 0.2)
    with pytest.raises(GroupNotStopped) as info:
        group("exec sleep 5", timeout=0.3)
    monkeypatch.undo()
    # Unreaped, so the group id is still this test's to signal.
    os.killpg(info.value.pgid, signal.SIGKILL)
    assert wait_gone(info.value.pgid)


def test_run_group_restores_the_signal_handlers() -> None:
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGHUP)}
    group("exit 0")
    assert {signum: signal.getsignal(signum) for signum in before} == before


_TERMINATE_SCRIPT = """
import signal, sys
from termctl import cli
from termctl.gateway import SystemGateway

signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
marker = sys.argv[1]

def run(*args, **kwargs):
    script = f"sleep 10 & echo $! > '{marker}.tmp'; mv '{marker}.tmp' '{marker}'; wait"
    SystemGateway().run_group(
        ["/bin/sh", "-c", script],
        stdin=b"",
        env={"PATH": "/usr/bin:/bin"},
        timeout=20,
        stdout_limit=0,
        stderr_limit=100,
    )
    return 0

cli._run = run
sys.exit(cli.main([]))
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_stop_signal_during_a_hook_kills_its_group(tmp_path: Path, signum: int) -> None:
    marker = tmp_path / "child"
    proc = subprocess.Popen(
        [sys.executable, "-c", _TERMINATE_SCRIPT, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        child = int(read_when_ready(marker))
        proc.send_signal(signum)  # the exact process this test started
        _, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 128 + signum
    assert stderr == f"termctl: terminated by signal {signum}\n"
    assert wait_gone(child)


# --- spawn_watched ------------------------------------------------------------------------------


def watched(script: str, *, timeout: float = 10, stdin: bytes = b"", **env: str) -> int:
    return SystemGateway().spawn_watched(
        [SH, "-c", script], stdin=stdin, env={**base_env(), **env}, timeout=timeout
    )


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_runs_the_hook_detached_with_stdin_and_env(tmp_path: Path) -> None:
    out = tmp_path / "out"
    script = (
        f"{{ cat; echo \"$X\"; echo $$; ps -o pgid= -p $$; ps -o pgid= -p $PPID; }} > '{out}.tmp'; "
        f"mv '{out}.tmp' '{out}'"
    )
    start = time.monotonic()
    watchdog = watched(script, stdin=b"payload\n", X="y")
    assert time.monotonic() - start < 2
    lines = read_when_ready(out).split("\n")
    assert lines[:2] == ["payload", "y"]
    hook_pid, hook_pgid, watchdog_pgid = (int(field) for field in lines[2:5])
    assert hook_pgid == hook_pid  # the hook leads a group of its own
    assert watchdog_pgid == watchdog != os.getpgrp()
    assert reap(watchdog)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_kills_the_hook_group_at_its_timeout(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = f"sleep 10 & echo $$ $! > '{pids}.tmp'; mv '{pids}.tmp' '{pids}'; sleep 10"
    start = time.monotonic()
    watchdog = watched(script, timeout=1)
    assert time.monotonic() - start < 2  # termctl does not wait
    assert os.getsid(watchdog) == watchdog  # a session of its own
    leader, child = (int(pid) for pid in read_when_ready(pids).split())
    assert wait_gone(leader, child)
    assert reap(watchdog)
    assert time.monotonic() - start < 5


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_kills_a_leader_that_left_its_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        f"exec '{sys.executable}' -c 'import os, sys, time; "
        "os.setpgid(0, os.getpgid(os.getppid())); "
        'open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(10)\' '
        f"'{pid_file}'"
    )
    watchdog = watched(script, timeout=1)
    assert wait_until(lambda: pid_file.exists() and pid_file.read_text() != "")
    assert wait_gone(int(pid_file.read_text()))
    assert reap(watchdog)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_kills_the_hook_group_when_the_watchdog_is_stopped(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = f"sleep 10 & echo $$ $! > '{pids}.tmp'; mv '{pids}.tmp' '{pids}'; sleep 10"
    watchdog = watched(script, timeout=30)
    leader, child = (int(pid) for pid in read_when_ready(pids).split())
    assert os.waitpid(watchdog, os.WNOHANG) == (0, 0)  # still this test's running child
    os.kill(watchdog, signal.SIGTERM)
    assert wait_gone(leader, child)
    assert reap(watchdog)


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_ignores_pythonpath_and_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A planted termctl package must not replace the watchdog.
    planted = tmp_path / "termctl"
    planted.mkdir()
    hijacked = tmp_path / "hijacked"
    (planted / "__init__.py").write_text("")
    (planted / "hookwatch.py").write_text(f"open({str(hijacked)!r}, 'w').close()\n")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out"
    watchdog = watched(f"echo ran > '{out}.tmp'; mv '{out}.tmp' '{out}'", PYTHONPATH=str(tmp_path))
    assert read_when_ready(out) == "ran\n"
    assert reap(watchdog)
    assert not hijacked.exists()


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_resets_an_ignored_sigchld(tmp_path: Path) -> None:
    out = tmp_path / "out"
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        watchdog = watched(f"echo ran > '{out}.tmp'; mv '{out}.tmp' '{out}'")
        assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGCHLD, previous)
    assert read_when_ready(out) == "ran\n"
    assert reap(watchdog)


@pytest.mark.parametrize("argv", [[], ["1"], ["0", SH], ["-1", SH], ["x", SH], ["nan", SH]])
def test_watchdog_rejects_bad_arguments(argv: list[str]) -> None:
    # Checked before the watchdog touches any signal handler, so this runs in-process.
    assert hookwatch.main(argv) == 2
