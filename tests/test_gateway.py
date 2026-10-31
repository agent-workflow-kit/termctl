from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
import time
from datetime import UTC
from pathlib import Path

import pytest

from termctl.gateway import BoundedOutput, Clock, Gateway, SystemClock, SystemGateway

from .fakes import FakeClock, FakeGateway


def test_system_gateway_runs_argv_without_a_shell() -> None:
    result = SystemGateway().run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME;"])
    assert (result.returncode, result.stdout) == (0, "$HOME;\n")


def test_system_gateway_passes_stdin_and_env() -> None:
    script = "import os, sys; print(sys.stdin.read() + os.environ['X'])"
    result = SystemGateway().run([sys.executable, "-c", script], stdin="a", env={"X": "b"})
    assert result.stdout == "ab\n"


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
