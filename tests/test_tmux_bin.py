"""tmux binary resolution and the version check (interface.md "Config", "Errors": tmux version)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from termctl import tmux_bin
from termctl.errors import TermctlError
from termctl.gateway import SystemGateway
from termctl.tmux_bin import TmuxBinary, TmuxVersion

from .fakes import TMUX, FakeGateway, completed

# --- version parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("tmux 3.2a\n", (3, 2, "3.2")),
        ("tmux 3.5a\n", (3, 5, "3.5")),
        ("tmux 3.2\n", (3, 2, "3.2")),
        ("tmux 3.3a", (3, 3, "3.3")),
        ("tmux next-3.6\n", (3, 6, "3.6")),
        ("tmux next-3.4\n", (3, 4, "3.4")),
        ("tmux openbsd-7.4\n", (7, 4, "7.4")),
        ("tmux 3.1c\n", (3, 1, "3.1")),
        ("tmux 2.9\n", (2, 9, "2.9")),
        ("tmux 1.8\n", (1, 8, "1.8")),
        ("tmux 10.0\n", (10, 0, "10.0")),
        ("tmux 3.10\n", (3, 10, "3.10")),
        # The first match wins, even when a later number looks like a version.
        ("tmux 3.1 (patched to 3.5)\n", (3, 1, "3.1")),
        ("tmux 3.5.1\n", (3, 5, "3.5")),
        # Leading zeros are part of the number, not octal.
        ("tmux 03.02\n", (3, 2, "03.02")),
        # A match need not be at the start of the line or the output.
        ("\n\nversion: 3.4\n", (3, 4, "3.4")),
    ],
)
def test_parse_version(output: str, expected: tuple[int, int, str]) -> None:
    assert tmux_bin.parse_version(output) == TmuxVersion(*expected)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "\n",
        "tmux master\n",
        "tmux next\n",
        "tmux 3\n",
        "tmux .5\n",
        "tmux 3.\n",
        "garbage \x00\x1b[31m\n",
        "tmux 3,2\n",
        # Only ASCII digits count: these are Unicode digits (fullwidth, Arabic-Indic).
        "tmux \uff13.\uff12\n",
        "tmux \u0663.\u0662\n",
    ],
)
def test_parse_version_without_a_match_is_none(output: str) -> None:
    assert tmux_bin.parse_version(output) is None


def test_parse_version_huge_numbers_compare_as_huge() -> None:
    # Beyond Python's int-from-string digit limit; must neither raise nor wrap around.
    version = tmux_bin.parse_version("tmux " + "9" * 10_000 + ".0\n")
    assert version is not None
    assert version >= tmux_bin.FLOOR
    assert tmux_bin.parse_version("tmux 3." + "9" * 10_000) >= tmux_bin.FLOOR  # type: ignore[operator]


def test_parse_version_huge_zero_padded_numbers_keep_their_value() -> None:
    version = tmux_bin.parse_version("tmux " + "0" * 10_000 + "3.1\n")
    assert version is not None and (version.major, version.minor) == (3, 1)
    assert version < tmux_bin.FLOOR


def test_parse_version_is_linear_on_long_digit_runs() -> None:
    # A naive `[0-9]+\.[0-9]+` search backtracks quadratically on a long run of digits
    # with no dot; 200k digits would take minutes.
    output = "tmux " + "1" * 200_000 + "x"
    assert tmux_bin.parse_version(output) is None
    assert tmux_bin.parse_version(output + " 3.4") == TmuxVersion(3, 4, "3.4")


def test_parse_version_huge_output() -> None:
    assert tmux_bin.parse_version("x" * 1_000_000 + " 2.8") == TmuxVersion(2, 8, "2.8")


@pytest.mark.parametrize(
    ("version", "new_enough"),
    [((3, 2), True), ((3, 1), False), ((2, 9), False), ((3, 10), True), ((4, 0), True)],
)
def test_floor_comparison(version: tuple[int, int], new_enough: bool) -> None:
    parsed = TmuxVersion(*version, f"{version[0]}.{version[1]}")
    assert (parsed >= tmux_bin.FLOOR) is new_enough


# --- binary resolution --------------------------------------------------------------------------


def test_resolve_command_name_on_path_returns_first_executable() -> None:
    gateway = FakeGateway(executables={"/b/tmux", "/c/tmux"})
    environ = {"PATH": "/a:/b:/c"}
    assert tmux_bin.resolve("tmux", environ, gateway) == "/b/tmux"
    assert gateway.executable_checks == ["/a/tmux", "/b/tmux"]


def test_resolve_absolute_path_is_kept_without_a_path_search() -> None:
    gateway = FakeGateway(executables={"/opt/tmux/bin/tmux"})
    assert tmux_bin.resolve("/opt/tmux/bin/tmux", {}, gateway) == "/opt/tmux/bin/tmux"
    assert gateway.executable_checks == ["/opt/tmux/bin/tmux"]


def test_resolve_other_names_too() -> None:
    gateway = FakeGateway(executables={"/usr/local/bin/tmux-3.5"})
    environ = {"PATH": "/usr/local/bin"}
    assert tmux_bin.resolve("tmux-3.5", environ, gateway) == "/usr/local/bin/tmux-3.5"


def assert_missing(name: str, environ: dict[str, str], gateway: FakeGateway) -> TermctlError:
    with pytest.raises(TermctlError) as caught:
        tmux_bin.resolve(name, environ, gateway)
    assert caught.value.code == "tmux_missing"
    assert caught.value.exit_code == 12
    assert caught.value.details == {"tmux_bin": name}
    return caught.value


def test_missing_on_path() -> None:
    err = assert_missing("tmux", {"PATH": "/a:/b"}, FakeGateway(executables=set()))
    assert "tmux" in err.message


def test_missing_absolute_path() -> None:
    assert_missing("/opt/tmux", {"PATH": "/usr/bin"}, FakeGateway(executables={"/usr/bin/tmux"}))


def test_no_path_variable_is_missing_not_a_default_path() -> None:
    gateway = FakeGateway(executables={"/usr/bin/tmux", "/bin/tmux"})
    assert_missing("tmux", {}, gateway)
    assert gateway.executable_checks == []


def test_empty_path_is_missing() -> None:
    gateway = FakeGateway(executables={"/usr/bin/tmux"})
    assert_missing("tmux", {"PATH": ""}, gateway)
    assert gateway.executable_checks == []


def test_relative_and_empty_path_entries_are_skipped() -> None:
    # An empty entry means the current directory to a shell; a relative entry depends on it.
    # Neither gives an absolute path, so neither is searched.
    gateway = FakeGateway(executables={"/usr/bin/tmux"})
    environ = {"PATH": ":bin:./x:/usr/bin:"}
    assert tmux_bin.resolve("tmux", environ, gateway) == "/usr/bin/tmux"
    assert gateway.executable_checks == ["/usr/bin/tmux"]


@pytest.mark.parametrize("name", ["bin/tmux", "./tmux", "../tmux", "tmux/", "."])
def test_relative_path_with_a_slash_is_refused(name: str) -> None:
    gateway = FakeGateway(executables={"/usr/bin/tmux", "/usr/bin/bin/tmux"})
    assert_missing(name, {"PATH": "/usr/bin"}, gateway)
    assert gateway.executable_checks == []


@pytest.mark.parametrize("name", ["..", "tmux\x00x", "tmux\n"])
def test_odd_names_are_refused_or_missing(name: str) -> None:
    gateway = FakeGateway(executables={"/usr/bin/tmux", "/usr/bin/.."})
    assert_missing(name, {"PATH": "/usr/bin"}, gateway)


def test_path_entries_are_joined_without_doubling_slashes() -> None:
    gateway = FakeGateway(executables={"/usr/bin/tmux"})
    assert tmux_bin.resolve("tmux", {"PATH": "/usr/bin/"}, gateway) == "/usr/bin/tmux"


def test_unicode_and_spaces_in_path_entries() -> None:
    gateway = FakeGateway(executables={"/opt/ü dir/tmux"})
    assert tmux_bin.resolve("tmux", {"PATH": "/opt/ü dir"}, gateway) == "/opt/ü dir/tmux"


# --- the version check --------------------------------------------------------------------------


def test_check_version_accepts_the_floor() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.2a\n")})
    version, warning = tmux_bin.check_version(TMUX, gateway)
    assert version == TmuxVersion(3, 2, "3.2")
    assert warning is None
    assert gateway.calls == [[TMUX, "-V"]]


@pytest.mark.parametrize("output", ["tmux 3.1c\n", "tmux 2.6\n", "tmux next-3.1\n"])
def test_check_version_below_the_floor_is_too_old(output: str) -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed(output)})
    with pytest.raises(TermctlError) as caught:
        tmux_bin.check_version(TMUX, gateway)
    err = caught.value
    assert (err.code, err.exit_code) == ("tmux_too_old", 12)
    assert "3.2" in err.message
    assert err.details is not None and err.details["tmux_bin"] == TMUX


@pytest.mark.parametrize(
    "output", ["tmux master\n", "", "garbage\n", "x" * 100_000, "\x1b]0;evil\x07\n"]
)
def test_check_version_without_a_match_warns_and_continues(output: str) -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed(output)})
    version, warning = tmux_bin.check_version(TMUX, gateway)
    assert version is None
    assert warning is not None and "assuming" in warning
    # The warning quotes at most a short prefix of what tmux printed.
    assert len(warning) < 300


def test_check_version_reads_stdout_only() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux master\n", stderr="tmux 2.1\n")})
    version, warning = tmux_bin.check_version(TMUX, gateway)
    assert (version, warning is not None) == (None, True)


def test_check_version_nonzero_exit_is_missing() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.5a\n", returncode=1)})
    with pytest.raises(TermctlError) as caught:
        tmux_bin.check_version(TMUX, gateway)
    assert caught.value.code == "tmux_missing"


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(13, "Permission denied"),
        OSError(8, "Exec format error"),
        FileNotFoundError(2, "No such file or directory"),
        subprocess.TimeoutExpired([TMUX, "-V"], 10),
    ],
)
def test_check_version_unrunnable_binary_is_missing(exc: Exception) -> None:
    gateway = FakeGateway({(TMUX, "-V"): exc})
    with pytest.raises(TermctlError) as caught:
        tmux_bin.check_version(TMUX, gateway)
    assert caught.value.code == "tmux_missing"
    assert caught.value.details == {"tmux_bin": TMUX}


def test_check_version_undecodable_output_warns_and_continues() -> None:
    exc = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    gateway = FakeGateway({(TMUX, "-V"): exc})
    version, warning = tmux_bin.check_version(TMUX, gateway)
    assert version is None and warning is not None


def test_check_version_runs_with_a_timeout_and_an_empty_environment() -> None:
    seen: dict[str, object] = {}

    class Recording(FakeGateway):
        def run(self, argv, *, stdin=None, timeout=None, env=None):  # type: ignore[no-untyped-def]
            seen.update(timeout=timeout, env=env, stdin=stdin)
            return super().run(argv, stdin=stdin, timeout=timeout, env=env)

    tmux_bin.check_version(TMUX, Recording())
    assert seen == {"timeout": tmux_bin.VERSION_TIMEOUT, "env": {}, "stdin": None}


# --- probe: resolution, then the version, once --------------------------------------------------


def test_probe_resolves_then_checks_the_version() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.5a\n")})
    warnings: list[str] = []
    result = tmux_bin.probe("tmux", {"PATH": "/usr/bin"}, gateway, warnings.append)
    assert result == TmuxBinary(TMUX, TmuxVersion(3, 5, "3.5"))
    assert result.version_text == "3.5"
    assert warnings == []
    assert gateway.calls == [[TMUX, "-V"]]


def test_probe_reports_the_warning() -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux master\n")})
    warnings: list[str] = []
    result = tmux_bin.probe("tmux", {"PATH": "/usr/bin"}, gateway, warnings.append)
    assert result == TmuxBinary(TMUX, None)
    assert result.version_text is None
    assert len(warnings) == 1


def test_probe_missing_binary_never_runs_anything() -> None:
    gateway = FakeGateway(executables=set())
    with pytest.raises(TermctlError) as caught:
        tmux_bin.probe("tmux", {"PATH": "/usr/bin"}, gateway, lambda _: None)
    assert caught.value.code == "tmux_missing"
    assert gateway.calls == []


# --- the real gateway and a real tmux -----------------------------------------------------------


def make_file(path: Path, mode: int) -> str:
    path.write_text("#!/bin/sh\necho 'tmux 3.4'\n")
    path.chmod(mode)
    return str(path)


def test_system_gateway_is_executable_file(tmp_path: Path) -> None:
    gateway = SystemGateway()
    assert gateway.is_executable_file(make_file(tmp_path / "x", 0o755))
    assert not gateway.is_executable_file(make_file(tmp_path / "noexec", 0o644))
    assert not gateway.is_executable_file(str(tmp_path))
    assert not gateway.is_executable_file(str(tmp_path / "missing"))
    assert not gateway.is_executable_file(str(tmp_path / "x") + "\x00")
    (tmp_path / "link").symlink_to(tmp_path / "x")
    assert gateway.is_executable_file(str(tmp_path / "link"))
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    assert not gateway.is_executable_file(str(tmp_path / "dangling"))
    os.mkfifo(tmp_path / "fifo", 0o755)
    assert stat.S_ISFIFO(os.stat(tmp_path / "fifo").st_mode)
    assert not gateway.is_executable_file(str(tmp_path / "fifo"))


def test_probe_with_the_system_gateway_and_a_script(tmp_path: Path) -> None:
    make_file(tmp_path / "tmux", 0o755)
    (tmp_path / "first").mkdir()
    make_file(tmp_path / "first" / "tmux", 0o644)
    environ = {"PATH": f"{tmp_path / 'nope'}:{tmp_path / 'first'}:{tmp_path}"}
    result = tmux_bin.probe("tmux", environ, SystemGateway(), lambda _: None)
    assert result == TmuxBinary(str(tmp_path / "tmux"), TmuxVersion(3, 4, "3.4"))


REAL_TMUX = shutil.which("tmux")


@pytest.mark.tmux
def test_probe_real_tmux() -> None:
    if REAL_TMUX is None:
        if os.environ.get("TERMCTL_REQUIRE_TMUX"):
            pytest.fail("tmux is not installed, and TERMCTL_REQUIRE_TMUX requires the tmux tests")
        pytest.skip("tmux is not installed")
    environ = {"PATH": os.path.dirname(REAL_TMUX)}
    result = tmux_bin.probe("tmux", environ, SystemGateway(), lambda _: None)
    assert result.path == REAL_TMUX
    assert os.path.isabs(result.path)
    assert result.version is not None and result.version >= tmux_bin.FLOOR
