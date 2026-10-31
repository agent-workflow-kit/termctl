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

from termctl.gateway import Clock, Gateway, GroupRun, SystemClock, SystemGateway

from .fakes import FakeClock, FakeGateway


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


# --- run_group and spawn_group ------------------------------------------------------------------

SH = "/bin/sh"


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
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")} if env is None else env,
        timeout=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )


def group_is_gone(pgid: int) -> bool:
    """Whether no process is left in ``pgid``. A killed grandchild is a zombie until init
    reaps it, so this waits up to 5 seconds."""
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


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
    # The grandchild keeps running after sh is killed unless the group is signaled.
    script = f"(sleep 2; touch '{marker}') & echo started; echo partial >&2; sleep 30"
    start = time.monotonic()
    run = group(script, timeout=0.5)
    assert time.monotonic() - start < 5
    assert run.timed_out and run.returncode is None
    assert run.stdout == b"started\n" and run.stderr == b"partial\n"
    assert group_is_gone(run.pgid)
    time.sleep(2.5)
    assert not marker.exists()


def test_run_group_timeout_when_a_grandchild_holds_the_pipes(tmp_path: Path) -> None:
    # sh exits at once, but its background child keeps stdout open.
    run = group("sleep 30 & exit 0", timeout=0.5)
    assert run.timed_out
    assert group_is_gone(run.pgid)


def test_run_group_reports_a_signal_as_a_negative_returncode() -> None:
    run = group("kill -TERM $$")
    assert run.returncode == -signal.SIGTERM


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
        group("sleep 30", timeout=10)
    assert group_is_gone(seen[0])


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_spawn_group_runs_detached_with_stdin(tmp_path: Path) -> None:
    out = tmp_path / "out"
    start = time.monotonic()
    pgid = SystemGateway().spawn_group(
        [SH, "-c", f"cat > '{out}.tmp'; echo \"$X\" >> '{out}.tmp'; mv '{out}.tmp' '{out}'"],
        stdin=b"payload\n",
        env={"X": "y", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert time.monotonic() - start < 2
    assert pgid != os.getpgrp()
    for _ in range(100):
        if out.exists():
            break
        time.sleep(0.05)
    assert out.read_bytes() == b"payload\ny\n"
