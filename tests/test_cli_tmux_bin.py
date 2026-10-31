"""The tmux checks of the check order (interface.md "Check order" step 3, "Errors")."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from termctl import cli
from termctl.cli import Context, Output, Result
from termctl.errors import TermctlError
from termctl.tmux_bin import TmuxBinary, TmuxVersion

from .conftest import private_environ
from .fakes import TMUX, FakeClock, FakeGateway, completed
from .schemas import validate
from .test_cli import ENVIRON, ID, MINIMAL, NO_CONFIG, Run

GOLDEN = Path(__file__).parent / "golden"
TMUX_COMMANDS = sorted(command for command in MINIMAL if command != "placements")


def capture(monkeypatch: pytest.MonkeyPatch) -> list[Context]:
    seen: list[Context] = []

    def run_command(args: Any, ctx: Context) -> Result:
        seen.append(ctx)
        return Result({}, "")

    monkeypatch.setattr(cli, "run_command", run_command)
    return seen


@pytest.mark.parametrize("command", TMUX_COMMANDS)
def test_missing_tmux_exits_12(command: str) -> None:
    run = Run(["--json", *MINIMAL[command]], FakeGateway(executables=set()))
    assert run.code == 12
    envelope = run.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "tmux_missing"
    assert run.gateway.calls == []


@pytest.mark.parametrize("command", TMUX_COMMANDS)
def test_old_tmux_exits_12(command: str) -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.1c\n")})
    run = Run(["--json", *MINIMAL[command]], gateway)
    assert run.code == 12
    envelope = run.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "tmux_too_old"
    assert run.gateway.calls == [[TMUX, "-V"]]


def test_placements_never_checks_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture(monkeypatch)
    run = Run(["placements"], FakeGateway(executables=set()), environ=NO_CONFIG)
    assert run.code == 0
    assert seen[0].tmux is None
    assert run.gateway.executable_checks == [] and run.gateway.calls == []


def test_missing_golden() -> None:
    expected = json.loads((GOLDEN / "error-tmux-missing.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert Run(["--json", "show", ID], FakeGateway(executables=set())).json() == expected


def test_too_old_golden() -> None:
    expected = json.loads((GOLDEN / "error-tmux-too-old.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.1c\n")})
    assert Run(["--json", "show", ID], gateway).json() == expected


def test_missing_human_output() -> None:
    run = Run(["show", ID], FakeGateway(executables=set()))
    assert (run.code, run.stdout) == (12, "")
    assert run.stderr == "termctl: tmux not found: no executable tmux on PATH\n"


def test_resolved_and_kept_on_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture(monkeypatch)
    gateway = FakeGateway(
        {("/a/tmux", "-V"): completed("tmux 3.4\n")}, executables={"/a/tmux", TMUX}
    )
    run = Run(["list"], gateway, environ={**ENVIRON, "PATH": "/x:/a:/usr/bin"})
    assert run.code == 0
    assert seen[0].tmux == TmuxBinary("/a/tmux", TmuxVersion(3, 4, "3.4"))
    assert gateway.executable_checks == ["/x/tmux", "/a/tmux"]
    assert gateway.calls == [["/a/tmux", "-V"]]


def test_check_tmux_probes_only_once() -> None:
    gateway = FakeGateway()
    out = Output(stdout=io.StringIO(), stderr=io.StringIO())
    ctx = Context(gateway=gateway, clock=FakeClock(), environ=private_environ(ENVIRON), out=out)
    args = cli.parse_args(["show", ID])
    cli.resolve_server(args, ctx)
    cli.check_tmux(args, ctx)
    first = ctx.tmux
    cli.check_tmux(args, ctx)
    assert first == TmuxBinary(TMUX, TmuxVersion(3, 5, "3.5"))
    assert ctx.tmux is first
    assert ctx.tmux_path == TMUX
    assert gateway.calls == [[TMUX, "-V"]]
    assert gateway.executable_checks == [TMUX]


def test_earlier_resolution_is_reused_and_the_version_still_checked() -> None:
    # `open` resolves tmux_bin in step 2 of its order; step 3 must not resolve it again,
    # and must not skip `tmux -V`.
    gateway = FakeGateway({("/a/tmux", "-V"): completed("tmux 3.1\n")}, executables=set())
    out = Output(stdout=io.StringIO(), stderr=io.StringIO())
    ctx = Context(gateway=gateway, clock=FakeClock(), environ={}, out=out)
    ctx.tmux_path = "/a/tmux"
    with pytest.raises(TermctlError) as caught:
        cli.check_tmux(cli.parse_args(["show", ID]), ctx)
    assert caught.value.code == "tmux_too_old"
    assert caught.value.details == {"tmux_bin": "tmux", "path": "/a/tmux", "tmux_version": "3.1"}
    assert gateway.executable_checks == []
    assert gateway.calls == [["/a/tmux", "-V"]]
    assert ctx.tmux is None


def test_version_query_gets_the_filtered_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # interface.md "Environment": every tmux command, `tmux -V` included, runs with the
    # caller's environment filtered by server_env_allow; the default keeps the locale.
    capture(monkeypatch)
    environ = {**ENVIRON, "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "SECRET": "x"}
    run = Run(["list"], environ=environ)
    state_home = os.environ["XDG_STATE_HOME"]  # the test's private one (conftest)
    assert run.gateway.envs == [
        {
            **NO_CONFIG,
            "PATH": "/usr/bin",
            "LANG": "de_DE.UTF-8",
            "LC_ALL": "de_DE.UTF-8",
            "XDG_STATE_HOME": state_home,
        }
    ]


def test_failed_version_query_escapes_tmux_stderr() -> None:
    stderr = "need UTF-8 locale\x1b[2J\n"
    gateway = FakeGateway({(TMUX, "-V"): completed("", stderr=stderr, returncode=1)})
    run = Run(["show", ID], gateway)
    assert run.code == 12
    assert run.stderr == (
        "termctl: cannot run /usr/bin/tmux -V: exit status 1 'need UTF-8 locale\\x1b[2J'\n"
    )


def test_configured_tmux_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture(monkeypatch)
    gateway = FakeGateway(
        {("/opt/tmux", "-V"): completed("tmux 3.2a\n")}, executables={"/opt/tmux"}
    )
    run = Run(["list"], gateway, environ={**ENVIRON, "TERMCTL_TMUX_BIN": "/opt/tmux"})
    assert run.code == 0
    assert seen[0].tmux == TmuxBinary("/opt/tmux", TmuxVersion(3, 2, "3.2"))


def test_unparsable_version_warns_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture(monkeypatch)
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux master\x1b[2J\n")})
    run = Run(["list"], gateway)
    assert run.code == 0
    assert seen[0].tmux == TmuxBinary(TMUX, None)
    assert run.stderr == (
        "termctl: warning: no version in /usr/bin/tmux -V output 'tmux master\\x1b[2J'; "
        "assuming tmux 3.2 or newer\n"
    )
    assert Run(["list", "--quiet"], gateway).stderr == ""


def test_verbose_traces_the_version_query(monkeypatch: pytest.MonkeyPatch) -> None:
    capture(monkeypatch)
    assert Run(["list", "--verbose"]).stderr == "termctl: tmux: /usr/bin/tmux -V\n"
    assert Run(["list"]).stderr == ""


def test_usage_errors_come_before_the_tmux_checks() -> None:
    run = Run(["--json", "show", "not an id"], FakeGateway(executables=set()))
    assert run.code == 2
    assert run.gateway.executable_checks == []


def test_config_errors_come_before_the_tmux_checks() -> None:
    gateway = FakeGateway(executables=set())
    run = Run(["--json", "show", ID], gateway, environ={**ENVIRON, "TERMCTL_TMUX_BIN": ""})
    assert (run.code, run.json()["error"]["code"]) == (2, "config_invalid")
    assert gateway.executable_checks == []
