from __future__ import annotations

import ast
import os
import pwd
import sys
from datetime import UTC
from pathlib import Path

import pytest

import termctl
from termctl.gateway import Clock, Gateway, SystemClock, SystemGateway

from .fakes import FakeClock, FakeGateway


def test_system_gateway_runs_argv_without_a_shell() -> None:
    result = SystemGateway().run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME;"])
    assert (result.returncode, result.stdout) == (0, "$HOME;\n")


def test_system_gateway_passes_stdin_and_env() -> None:
    script = "import os, sys; print(sys.stdin.read() + os.environ['X'])"
    result = SystemGateway().run([sys.executable, "-c", script], stdin="a", env={"X": "b"})
    assert result.stdout == "ab\n"


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
