from __future__ import annotations

import ast
import os
import pwd
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

import termctl
from termctl import gateway as gateway_module
from termctl import hookwatch
from termctl.gateway import (
    BoundedOutput,
    Clock,
    Gateway,
    GroupNotStopped,
    GroupRun,
    SystemClock,
    SystemGateway,
    Terminated,
    started_pgid,
    stop_signals,
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


def test_run_starts_the_program_in_a_process_group_of_its_own() -> None:
    # Review round 2, R2-1: a signal sent to termctl's process group (timeout, a terminal
    # hangup) must not kill a tmux client halfway through a command chain.
    script = (
        "import os; print(os.getpgrp() == os.getpid(), os.getpgrp() != os.getpgid(os.getppid()))"
    )
    result = SystemGateway().run([sys.executable, "-c", script])
    assert result.stdout == "True True\n"


def test_run_without_input_gives_the_program_no_terminal_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def run(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = SystemGateway().run([sys.executable, "-c", "import sys; print(sys.stdin.read())"])
    assert result.stdout == "\n"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["process_group"] == 0


# The child writes UTF-8 (a bell emoji and an accent) plus one invalid byte to stdout and
# stderr, and echoes stdin back as bytes.
_CHILD = (
    "import sys; data = sys.stdin.buffer.read();"
    "sys.stdout.buffer.write(data + b'caf\\xc3\\xa9 \\xf0\\x9f\\x94\\x94 \\xff');"
    "sys.stderr.buffer.write(b'\\xc3\\xa9\\xff')"
)
_IN_ASCII_LOCALE = """
import sys
from termctl.gateway import SystemGateway
r = SystemGateway().run([sys.executable, "-c", sys.argv[1]], stdin="\\u00e9\\udcfe")
print(ascii((sys.getfilesystemencoding(), r.stdout, r.stderr)))
"""


def test_system_gateway_reads_and_writes_utf8_whatever_the_locale() -> None:
    """tmux runs with -u and writes UTF-8, so output is decoded as UTF-8 (an undecodable byte
    kept as a lone surrogate) even when termctl runs in a non-UTF-8 locale."""
    src = str(Path(termctl.__file__).parent.parent)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONPATH": src,
    }
    result = SystemGateway().run(
        [sys.executable, "-c", _IN_ASCII_LOCALE, _CHILD], env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    fs_encoding, stdout, stderr = ast.literal_eval(result.stdout)
    if fs_encoding.lower().replace("-", "") == "utf8":
        pytest.skip("this host forces a UTF-8 filesystem encoding")
    assert stdout == "\u00e9\udcfecaf\u00e9 \U0001f514 \udcff"
    assert stderr == "\u00e9\udcff"


def write_script(path: Path, body: str) -> list[str]:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return [str(path)]


def assert_reaped(pid_file: Path) -> None:
    pid = int(pid_file.read_text())
    # A child that was killed but never waited for would linger as a zombie, which still
    # answers signal 0.
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_run_bounded_returns_the_output_and_status(tmp_path: Path) -> None:
    script = (
        "import os, sys\n"
        "sys.stdout.write('out ' + os.environ['X'])\n"
        "sys.stderr.write('err')\n"
        "sys.exit(3)\n"
    )
    argv = write_script(tmp_path / "s", script)
    result = SystemGateway().run_bounded(argv, env={"X": "b"}, timeout=30, limit=4096)
    assert result == BoundedOutput(3, b"out b", b"err", False)


def test_run_bounded_passes_only_the_given_environment_and_no_stdin(tmp_path: Path) -> None:
    script = "import os, sys; print(sorted(os.environ)); print(repr(sys.stdin.read()))"
    argv = write_script(tmp_path / "s", script)
    result = SystemGateway().run_bounded(argv, env={"A": "1"}, timeout=30, limit=4096)
    lines = result.stdout.decode().splitlines()
    assert [line for line in lines if not line.startswith("[")] == ["''"]
    assert "'A'" in lines[0] and "'HOME'" not in lines[0]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_run_bounded_stops_reading_at_the_limit_and_reaps_the_child(
    tmp_path: Path, stream: str
) -> None:
    pid_file = tmp_path / "pid"
    script = (
        "import os, sys\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"out = sys.{stream}.buffer\n"
        "while True:\n"
        "    out.write(b'x' * 65536)\n"
        "    out.flush()\n"
    )
    argv = write_script(tmp_path / "s", script)
    start = time.monotonic()
    result = SystemGateway().run_bounded(argv, env={}, timeout=60, limit=4096)
    # Endless output ends at the limit, long before the timeout.
    assert time.monotonic() - start < 30
    assert result.truncated
    assert len(getattr(result, stream)) == 4096
    assert result.returncode < 0
    assert_reaped(pid_file)


def test_run_bounded_times_out_and_reaps_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(600)\n"
    )
    argv = write_script(tmp_path / "s", script)
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        SystemGateway().run_bounded(argv, env={}, timeout=3, limit=4096)
    assert time.monotonic() - start < 30
    assert_reaped(pid_file)


def test_run_bounded_cannot_start(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SystemGateway().run_bounded([str(tmp_path / "missing")], env={}, timeout=5, limit=10)


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


def test_system_gateway_reads_output_faithfully() -> None:
    """No newline translation, and undecodable bytes are kept as lone surrogates (#11)."""
    script = (
        "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\xff\\xc3'); "
        "sys.stderr.buffer.write(b'e\\r\\xfe')"
    )
    result = SystemGateway().run([sys.executable, "-c", script])
    assert result.returncode == 0
    assert result.stdout == "a\r\nb\rc\udcff\udcc3"
    assert result.stderr == "e\r\udcfe"
    assert os.fsencode(result.stdout) == b"a\r\nb\rc\xff\xc3"


def test_system_gateway_decodes_utf8_output() -> None:
    script = "import sys; sys.stdout.buffer.write('é\U0001f514'.encode())"
    assert SystemGateway().run([sys.executable, "-c", script]).stdout == "é\U0001f514"


def test_system_gateway_encodes_stdin_as_utf8() -> None:
    script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    result = SystemGateway().run([sys.executable, "-c", script], stdin="é\n")
    assert result.stdout == "é\n"


def test_system_gateway_real_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 4242)
    assert SystemGateway().real_uid() == 4242


def test_system_gateway_lstat_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir(mode=0o750)
    (tmp_path / "dir").chmod(0o750)
    (tmp_path / "link").symlink_to(tmp_path / "dir")
    gateway = SystemGateway()
    info = gateway.lstat(str(tmp_path / "dir"))
    assert (stat.S_ISDIR(info.mode), stat.S_IMODE(info.mode)) == (True, 0o750)
    assert info.uid == os.getuid()
    assert stat.S_ISLNK(gateway.lstat(str(tmp_path / "link")).mode)
    with pytest.raises(FileNotFoundError):
        gateway.lstat(str(tmp_path / "missing"))


def test_system_gateway_real_path_is_strict(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "dir")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    gateway = SystemGateway()
    assert gateway.real_path(str(tmp_path / "link")) == os.path.realpath(tmp_path / "dir")
    with pytest.raises(OSError):
        gateway.real_path(str(tmp_path / "dangling"))
    with pytest.raises(OSError):
        gateway.real_path(str(tmp_path / "missing" / ".."))


def test_system_gateway_read_link(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to("relative/target")
    (tmp_path / "file").write_text("")
    gateway = SystemGateway()
    assert gateway.read_link(str(tmp_path / "link")) == "relative/target"
    with pytest.raises(OSError):
        gateway.read_link(str(tmp_path / "file"))
    with pytest.raises(FileNotFoundError):
        gateway.read_link(str(tmp_path / "missing"))


def test_system_gateway_make_dir_uses_the_mode(tmp_path: Path) -> None:
    path = tmp_path / "d"
    old = os.umask(0o022)
    try:
        SystemGateway().make_dir(str(path), 0o700)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_system_gateway_make_dir_refuses_an_existing_path(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        SystemGateway().make_dir(str(tmp_path / "link"), 0o700)
    assert not (tmp_path / "missing").exists()


def test_system_gateway_make_dir_never_creates_parents(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SystemGateway().make_dir(str(tmp_path / "a" / "b"), 0o700)


@pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is Linux-only")
def test_system_gateway_socket_peer_pid(tmp_path: Path) -> None:
    path = str(tmp_path / "sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        assert SystemGateway().socket_peer_pid(path) == os.getpid()


def test_system_gateway_socket_peer_pid_without_a_listener(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SystemGateway().socket_peer_pid(str(tmp_path / "missing"))


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


def _send_term_after(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[int]:
    """Make the next ``subprocess.Popen`` send this process SIGTERM right after it starts the
    program (or, with ``fail``, instead of starting it); return the started PIDs."""
    seen: list[int] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(subprocess, "Popen", real_popen)  # only the first call
        # Only with termctl's handler installed: SIGTERM must not reach the test runner itself.
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN)
        if fail:
            os.kill(os.getpid(), signal.SIGTERM)
            raise FileNotFoundError(2, "No such file or directory")
        proc = real_popen(*args, **kwargs)
        seen.append(proc.pid)
        os.kill(os.getpid(), signal.SIGTERM)  # this process: exact, and handled
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    return seen


def test_run_group_kills_the_group_on_a_stop_signal_while_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _send_term_after(monkeypatch)
    with pytest.raises(Terminated) as info:
        group("sleep 10", timeout=10)
    assert group_is_gone(seen[0])
    assert started_pgid(info.value) == seen[0]


def test_run_group_takes_the_stop_signals_over_from_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run's handler would raise at once; the hook's holds the signal while it starts.
    with stop_signals():
        outer = signal.getsignal(signal.SIGTERM)
        seen = _send_term_after(monkeypatch)
        with pytest.raises(Terminated) as info:
            group("sleep 10", timeout=10)
        assert signal.getsignal(signal.SIGTERM) == outer
    assert group_is_gone(seen[0])
    assert started_pgid(info.value) == seen[0]


def test_a_failure_to_start_carries_no_group() -> None:
    with pytest.raises(OSError) as info:
        SystemGateway().run_group(
            ["/nonexistent/program"], stdin=b"", env={}, timeout=1, stdout_limit=0,
            stderr_limit=0,
        )  # fmt: skip
    assert started_pgid(info.value) is None


STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def test_stop_signals_raise_for_a_run_and_restore_the_handlers() -> None:
    before = {signum: signal.getsignal(signum) for signum in STOPS}
    with stop_signals():
        assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN)
        with pytest.raises(Terminated) as info:
            os.kill(os.getpid(), signal.SIGTERM)  # this process: exact, and handled
            time.sleep(1)
    assert info.value.signum == signal.SIGTERM
    assert {signum: signal.getsignal(signum) for signum in STOPS} == before


@pytest.mark.parametrize("signum", STOPS)
def test_a_deferred_stop_signal_is_raised_when_the_block_ends(signum: int) -> None:
    done: list[str] = []
    with stop_signals() as signals:
        assert signal.getsignal(signum) not in (signal.SIG_DFL, signal.SIG_IGN)
        with pytest.raises((KeyboardInterrupt, Terminated)) as info:
            with signals.deferred():
                os.kill(os.getpid(), signum)  # this process: exact, and handled
                time.sleep(0.05)
                done.append("block")
            done.append("after")  # never reached
    assert done == ["block"]
    if signum == signal.SIGINT:
        assert isinstance(info.value, KeyboardInterrupt)
    else:
        assert isinstance(info.value, Terminated) and info.value.signum == signum


def test_an_error_in_a_deferred_block_wins_and_later_signals_are_ignored() -> None:
    with stop_signals() as signals:
        with pytest.raises(RuntimeError):
            with signals.deferred():
                os.kill(os.getpid(), signal.SIGTERM)  # this process: exact, and handled
                raise RuntimeError("step failed")
        os.kill(os.getpid(), signal.SIGTERM)  # ignored now
        time.sleep(0.05)


@pytest.mark.parametrize("signum", STOPS)
def test_run_group_holds_stop_signals_while_the_run_is_recorded(signum: int) -> None:
    # Review round 2, R2-2: a stop between the hook's end and the caller's bookkeeping must
    # not lose the hook; it is raised once the run is recorded, without a group to list again.
    recorded: list[GroupRun] = []

    def record(run: GroupRun) -> None:
        os.kill(os.getpid(), signum)  # this process: exact, and handled
        time.sleep(0.05)
        recorded.append(run)

    with stop_signals():
        with pytest.raises((KeyboardInterrupt, Terminated)) as info:
            SystemGateway().run_group(
                [SH, "-c", "exit 3"], stdin=b"", env=base_env(), timeout=10, stdout_limit=0,
                stderr_limit=0, record=record,
            )  # fmt: skip
    assert [run.returncode for run in recorded] == [3]
    assert started_pgid(info.value) is None


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_reports_the_watchdog_a_stop_signal_came_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with stop_signals():
        seen = _send_term_after(monkeypatch)
        with pytest.raises(Terminated) as info:
            watched("exit 0", timeout=5)
    assert started_pgid(info.value) == seen[0]
    assert reap(seen[0])


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_records_the_watchdog_before_raising_a_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[int] = []
    with stop_signals():
        seen = _send_term_after(monkeypatch)
        with pytest.raises(Terminated) as info:
            SystemGateway().spawn_watched(
                [SH, "-c", "exit 0"], stdin=b"", env=base_env(), timeout=5,
                record=recorded.append,
            )  # fmt: skip
    assert recorded == seen
    assert started_pgid(info.value) is None
    assert reap(seen[0])


def test_a_second_stop_signal_while_a_stopped_hook_unwinds_does_not_raise_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review round 2, R2-3: the hook's handler raised; the run's handler, restored, must not
    # raise a second stop that would replace the first (and its group).
    with stop_signals():
        seen = _send_term_after(monkeypatch)
        with pytest.raises(Terminated) as info:
            try:
                group("sleep 10", timeout=10)
            finally:
                os.kill(os.getpid(), signal.SIGHUP)  # this process: exact, and handled
                time.sleep(0.05)
    assert info.value.signum == signal.SIGTERM
    assert started_pgid(info.value) == seen[0]
    assert group_is_gone(seen[0])


def test_a_stop_signal_while_a_hook_is_killed_after_sigint_does_not_raise_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SIGINT reaches the run's handler while a hook runs; a SIGTERM during the kill that
    # follows must not replace the KeyboardInterrupt.
    real_stop = gateway_module._stop

    def stop(proc: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # this process: exact, and handled
        time.sleep(0.05)
        real_stop(proc)

    monkeypatch.setattr(gateway_module, "_stop", stop)
    seen: list[int] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(subprocess, "Popen", real_popen)  # only the first call
        # Only with termctl's handler installed: SIGINT must not reach the test runner itself.
        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
        proc = real_popen(*args, **kwargs)
        seen.append(proc.pid)
        os.kill(os.getpid(), signal.SIGINT)  # this process: exact, and handled
        return proc

    with stop_signals():
        monkeypatch.setattr(subprocess, "Popen", popen)
        with pytest.raises(KeyboardInterrupt) as info:
            group("sleep 10", timeout=10)
    assert started_pgid(info.value) == seen[0]
    assert group_is_gone(seen[0])


def test_stop_signals_leave_an_ignored_signal_alone() -> None:
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with stop_signals():
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_run_group_reports_a_stop_signal_that_came_while_it_failed_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _send_term_after(monkeypatch, fail=True)
    with pytest.raises(Terminated):
        group("exit 0")


def test_run_group_without_a_leader_watch_still_reports_the_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(self: Any) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(gateway_module._LeaderExit, "_watch", unavailable)
    assert group("exit 3").returncode == 3


def test_run_group_without_a_leader_watch_still_kills_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(self: Any) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(gateway_module._LeaderExit, "_watch", unavailable)
    seen: list[int] = []
    real_popen = subprocess.Popen

    def popen(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(subprocess, "Popen", real_popen)  # only the first call
        proc = real_popen(*args, **kwargs)
        seen.append(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    run = group("sleep 10", timeout=0.3)
    assert run.returncode is None
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


def test_ignored_hangups_stop_neither_the_run_nor_a_hook_it_runs() -> None:
    # close from its re-check on (closing termctl's own pane hangs up its terminal).
    with stop_signals() as signals:
        signals.ignore_hangups()
        os.kill(os.getpid(), signal.SIGHUP)  # this process: exact, and handled
        time.sleep(0.05)
        run = SystemGateway().run_group(
            [SH, "-c", "kill -HUP $PPID; sleep 0.2; echo done"], stdin=b"", env=base_env(),
            timeout=10, stdout_limit=100, stderr_limit=100,
        )  # fmt: skip
        assert run.stdout == b"done\n"
        # A program termctl runs gets SIGHUP at its default, and so does a hook.
        child = SystemGateway().run([SH, "-c", "kill -HUP $$; echo alive"])
        assert child.returncode == -signal.SIGHUP
        hook = SystemGateway().run_group(
            [SH, "-c", "kill -HUP $$; echo alive"], stdin=b"", env=base_env(), timeout=10,
            stdout_limit=100, stderr_limit=100,
        )  # fmt: skip
        assert hook.returncode == -signal.SIGHUP
        # SIGTERM still stops the run.
        with pytest.raises(Terminated):
            os.kill(os.getpid(), signal.SIGTERM)  # this process: exact, and handled
            time.sleep(1)


def test_an_ignored_hangup_in_a_deferred_block_is_not_raised_but_sigterm_is() -> None:
    with stop_signals() as signals:
        signals.ignore_hangups()
        with signals.deferred():
            os.kill(os.getpid(), signal.SIGHUP)  # this process: exact, and handled
            time.sleep(0.05)
        with pytest.raises(Terminated) as info:
            with signals.deferred():
                os.kill(os.getpid(), signal.SIGHUP)  # this process: exact, and handled
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)
    assert info.value.signum == signal.SIGTERM


def test_ignore_hangups_leaves_an_ignored_sighup_ignored_and_restores_the_handlers() -> None:
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with stop_signals() as signals:
            signals.ignore_hangups()
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, before)


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


def test_spawn_watched_runs_the_watchdog_by_its_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Proc:
        pid = 1234

    def popen(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert SystemGateway().spawn_watched([SH, "-c", "true"], stdin=b"", env={}, timeout=5) == 1234
    path = os.path.abspath(hookwatch.__file__)
    assert calls == [[sys.executable, "-I", path, "5", SH, "-c", "true"]]


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_watched_works_where_termctl_is_not_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The base interpreter of the test venv runs with -I, so it cannot import termctl (as with
    # a user-site install or PYTHONPATH); the watchdog must still run.
    base = getattr(sys, "_base_executable", sys.executable)
    probe = subprocess.run([base, "-I", "-c", "import termctl"], capture_output=True, check=False)
    if probe.returncode == 0:
        pytest.skip("termctl is importable by the base interpreter")
    monkeypatch.setattr(sys, "executable", base)
    out = tmp_path / "out"
    watchdog = watched(f"echo ran > '{out}.tmp'; mv '{out}.tmp' '{out}'")
    assert read_when_ready(out) == "ran\n"
    assert reap(watchdog)


_WATCHDOG_STOPPED_WHILE_STARTING = """
import os, signal, subprocess, sys
sys.path.insert(0, os.path.dirname(sys.argv[1]))
import hookwatch

real_popen = subprocess.Popen

def popen(*args, **kwargs):
    proc = real_popen(*args, **kwargs)
    with open(sys.argv[2] + ".tmp", "w") as f:
        f.write(str(proc.pid))
    os.rename(sys.argv[2] + ".tmp", sys.argv[2])
    os.kill(os.getpid(), signal.SIGTERM)  # this process, with the watchdog's handler
    return proc

subprocess.Popen = popen
sys.exit(hookwatch.main(["30", "/bin/sh", "-c", "sleep 10"]))
"""


def test_watchdog_kills_the_hook_on_a_stop_signal_while_it_starts(tmp_path: Path) -> None:
    marker = tmp_path / "pid"
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _WATCHDOG_STOPPED_WHILE_STARTING,
            os.path.abspath(hookwatch.__file__),
            str(marker),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    assert wait_gone(int(read_when_ready(marker)))


# --- tty writes (the bell, #20) -----------------------------------------------------------------


def test_write_tty_writes_to_a_terminal() -> None:
    master, slave = os.openpty()
    try:
        SystemGateway().write_tty(os.ttyname(slave), b"\a")
        assert os.read(master, 16) == b"\a"
    finally:
        os.close(master)
        os.close(slave)


def test_write_tty_opens_write_only_without_a_controlling_terminal_or_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[int] = []
    real_open = os.open

    def recording_open(path: str, flags: int, *args: Any) -> int:
        opened.append(flags)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", recording_open)
    master, slave = os.openpty()
    try:
        SystemGateway().write_tty(os.ttyname(slave), b"\a")
    finally:
        os.close(master)
        os.close(slave)
    [flags] = opened
    assert flags & os.O_ACCMODE == os.O_WRONLY
    for flag in (os.O_NOCTTY, os.O_NONBLOCK, os.O_CLOEXEC, os.O_NOFOLLOW):
        assert flags & flag
    assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)


def test_write_tty_never_writes_to_a_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"content")
    with pytest.raises(OSError, match="not a character device"):
        SystemGateway().write_tty(str(path), b"\a")
    assert path.read_bytes() == b"content"


def test_write_tty_never_creates_a_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SystemGateway().write_tty(str(tmp_path / "missing"), b"\a")
    assert not (tmp_path / "missing").exists()


def test_write_tty_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        SystemGateway().write_tty(str(tmp_path), b"\a")


def test_write_tty_never_blocks_on_a_fifo_without_a_reader(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    start = time.monotonic()
    with pytest.raises(OSError):
        SystemGateway().write_tty(str(path), b"\a")
    assert time.monotonic() - start < 5


def test_write_tty_never_writes_to_a_fifo_with_a_reader(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    os.mkfifo(path)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(OSError, match="not a character device"):
            SystemGateway().write_tty(str(path), b"\a")
        # The writer is closed again with nothing written: end of file, not a BEL.
        assert os.read(reader, 16) == b""
    finally:
        os.close(reader)


def test_write_tty_follows_no_trailing_symlink_to_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"content")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(OSError):
        SystemGateway().write_tty(str(tmp_path / "link"), b"\a")
    assert target.read_bytes() == b"content"


def test_write_tty_blocks_sigttou_while_writing_and_restores_the_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    masks: list[set[Any]] = []
    real_write = os.write

    def observed_write(fd: int, data: bytes) -> int:
        masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        return real_write(fd, data)

    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    monkeypatch.setattr(os, "write", observed_write)
    master, slave = os.openpty()
    try:
        SystemGateway().write_tty(os.ttyname(slave), b"\a")
    finally:
        os.close(master)
        os.close(slave)
    [during] = masks
    assert signal.SIGTTOU in during
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before


def test_write_tty_restores_the_mask_when_the_open_fails(tmp_path: Path) -> None:
    before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    with pytest.raises(FileNotFoundError):
        SystemGateway().write_tty(str(tmp_path / "missing"), b"\a")
    assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == before


# A session of its own with a private pty as its controlling terminal and tostop set; a forked
# child in a background process group writes the bell to it. Without SIGTTOU blocked the child
# would stop. Only the pty this script creates is written to.
_BACKGROUND_BELL = """
import fcntl, os, signal, sys, termios
from termctl.gateway import SystemGateway

master, slave = os.openpty()
fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
attrs = termios.tcgetattr(slave)
attrs[3] |= termios.TOSTOP
termios.tcsetattr(slave, termios.TCSANOW, attrs)
name = os.ttyname(slave)
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    try:
        SystemGateway().write_tty(name, b"\\a")
    except BaseException:
        os._exit(1)
    os._exit(0)
try:
    os.setpgid(pid, pid)
except OSError:
    pass  # the child already did it, or has exited
_, status = os.waitpid(pid, os.WUNTRACED)
if os.WIFSTOPPED(status):
    os.kill(pid, signal.SIGKILL)  # the exact child this script forked
    os.waitpid(pid, 0)
    print("stopped")
else:
    print(os.waitstatus_to_exitcode(status), repr(os.read(master, 16)))
"""


def test_write_tty_from_a_background_group_with_tostop_does_not_stop() -> None:
    src = str(Path(termctl.__file__).parent.parent)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": src}
    result = subprocess.run(
        [sys.executable, "-c", _BACKGROUND_BELL],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0 b'\\x07'\n"
