from __future__ import annotations

import os
import pwd
import stat
import sys
from datetime import UTC
from pathlib import Path

import pytest

from termctl.gateway import Clock, Gateway, SystemClock, SystemGateway

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
