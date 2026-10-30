from __future__ import annotations

import io
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from termctl import __version__, cli
from termctl.cli import Context, Result, main, parse_args
from termctl.errors import TermctlError, UsageError

from .fakes import FakeClock, FakeGateway
from .schemas import validate

GOLDEN = Path(__file__).parent / "golden"
# A config directory that does not exist, so tests never read the user's own config.
NO_CONFIG = {"XDG_CONFIG_HOME": str(Path(__file__).parent / "no-config")}
ID = "tc-abcdefgh"

# The shortest valid invocation of every command in interface.md "Commands".
MINIMAL: dict[str, list[str]] = {
    "open": ["open"],
    "show": ["show", ID],
    "exists": ["exists", ID],
    "list": ["list"],
    "rename": ["rename", ID, "new-name"],
    "mark": ["mark", ID, "attention"],
    "unmark": ["unmark", ID],
    "focus": ["focus", ID],
    "close": ["close", ID],
    "label add": ["label", "add", ID, "k=v"],
    "label remove": ["label", "remove", ID, "k=v"],
    "placements": ["placements"],
}

# Every global flag of interface.md "Global flags" except --version, with its parsed value.
GLOBAL_FLAGS: list[tuple[list[str], dict[str, Any]]] = [
    (["--json"], {"json": True}),
    (["--socket", "work"], {"socket": "work"}),
    (["--socket", "/run/user/tmux/sock"], {"socket": "/run/user/tmux/sock"}),
    (["--config", "/etc/termctl.yaml"], {"config": "/etc/termctl.yaml"}),
    (["--verbose"], {"verbose": True}),
    (["--quiet"], {"quiet": True}),
]

GLOBAL_DEFAULTS = {"json": False, "socket": None, "config": None, "verbose": False, "quiet": False}


class Run:
    """The outcome of one ``main`` call."""

    def __init__(self, argv: list[str], gateway: FakeGateway | None = None) -> None:
        self.gateway = gateway or FakeGateway()
        stdout, stderr = io.StringIO(), io.StringIO()
        self.code = main(
            argv,
            gateway=self.gateway,
            clock=FakeClock(),
            environ=NO_CONFIG,
            stdout=stdout,
            stderr=stderr,
        )
        self.stdout = stdout.getvalue()
        self.stderr = stderr.getvalue()

    def json(self) -> Any:
        return json.loads(self.stdout)


def assert_parsed(argv: list[str], expected: dict[str, Any]) -> None:
    args = vars(parse_args(argv))
    assert {key: args[key] for key in expected} == expected


# --- the command and flag surface -------------------------------------------------------------

SURFACE: list[tuple[list[str], dict[str, Any]]] = [
    (
        ["open"],
        {
            "command": "open",
            "name": None,
            "cwd": None,
            "env": [],
            "labels": [],
            "placement": None,
            "session": None,
            "target": None,
            "keep_on_exit": None,
            "focus": False,
            "dry_run": False,
            "actor": None,
            "decision_ids": [],
            "override_escalation": False,
            "hooks": [],
            "argv": [],
        },
    ),
    (
        [
            "open",
            "--name",
            "build",
            "--cwd",
            "src",
            "--env",
            "A=1",
            "--env",
            "B=2",
            "--label",
            "k=v",
            "--label",
            "x=y",
            "--placement",
            "work",
            "--session",
            "main",
            "--target",
            "pane",
            "--keep-on-exit",
            "failed",
            "--focus",
            "--dry-run",
            "--actor",
            "human:me",
            "--decision-id",
            "d-1",
            "--decision-id",
            "d-2",
            "--override-escalation",
            "--hook",
            "pre-open=true",
            "--hook",
            "post-open=false",
            "--",
            "make",
            "-j4",
        ],
        {
            "name": "build",
            "cwd": "src",
            "env": ["A=1", "B=2"],
            "labels": ["k=v", "x=y"],
            "placement": "work",
            "session": "main",
            "target": "pane",
            "keep_on_exit": "failed",
            "focus": True,
            "dry_run": True,
            "actor": "human:me",
            "decision_ids": ["d-1", "d-2"],
            "override_escalation": True,
            "hooks": ["pre-open=true", "post-open=false"],
            "argv": ["make", "-j4"],
        },
    ),
    (["open", "--target", "window"], {"target": "window"}),
    (["open", "--keep-on-exit", "never"], {"keep_on_exit": "never"}),
    (["open", "--keep-on-exit", "always"], {"keep_on_exit": "always"}),
    (["show", ID], {"command": "show", "id": ID}),
    (["exists", ID], {"command": "exists", "id": ID}),
    (["list"], {"command": "list", "labels": [], "session": None, "dead": False}),
    (
        ["list", "--label", "a=b", "--label", "c=d", "--session", "s", "--dead"],
        {"labels": ["a=b", "c=d"], "session": "s", "dead": True},
    ),
    (
        ["rename", ID, "new-name"],
        {"command": "rename", "id": ID, "name": "new-name", "dry_run": False},
    ),
    (["rename", ID, "n", "--dry-run"], {"dry_run": True}),
    (
        ["mark", ID, "attention"],
        {"command": "mark", "id": ID, "marker": "attention", "dry_run": False},
    ),
    (["mark", ID, "attention", "--dry-run"], {"dry_run": True}),
    (["unmark", ID], {"command": "unmark", "id": ID, "dry_run": False}),
    (["unmark", ID, "--dry-run"], {"dry_run": True}),
    (
        ["focus", ID],
        {"command": "focus", "id": ID, "client": None, "no_attach": False, "dry_run": False},
    ),
    (
        ["focus", ID, "--client", "/dev/pts/3", "--no-attach", "--dry-run"],
        {"client": "/dev/pts/3", "no_attach": True, "dry_run": True},
    ),
    (
        ["close", ID],
        {
            "command": "close",
            "id": ID,
            "force": False,
            "grace": None,
            "actor": None,
            "decision_ids": [],
            "override_escalation": False,
            "hooks": [],
            "dry_run": False,
        },
    ),
    (
        [
            "close",
            ID,
            "--force",
            "--grace",
            "30s",
            "--actor",
            "human:me",
            "--decision-id",
            "d-1",
            "--override-escalation",
            "--hook",
            "pre-close=true",
            "--hook",
            "post-close=true",
            "--dry-run",
        ],
        {
            "force": True,
            "grace": "30s",
            "actor": "human:me",
            "decision_ids": ["d-1"],
            "override_escalation": True,
            "hooks": ["pre-close=true", "post-close=true"],
            "dry_run": True,
        },
    ),
    (
        ["label", "add", ID, "a=b", "c=d"],
        {"command": "label add", "id": ID, "pairs": ["a=b", "c=d"], "dry_run": False},
    ),
    (["label", "add", ID, "a=b", "--dry-run"], {"dry_run": True}),
    (
        ["label", "remove", ID, "a=b"],
        {"command": "label remove", "id": ID, "pairs": ["a=b"], "dry_run": False},
    ),
    (["label", "remove", ID, "a=b", "--dry-run"], {"dry_run": True}),
    (["placements"], {"command": "placements", "labels": []}),
    (["placements", "--label", "a=b", "--label", "c=d"], {"labels": ["a=b", "c=d"]}),
]


@pytest.mark.parametrize(("argv", "expected"), SURFACE)
def test_command_surface(argv: list[str], expected: dict[str, Any]) -> None:
    assert_parsed(argv, expected)


def test_surface_covers_every_command() -> None:
    assert {expected.get("command") for _, expected in SURFACE} - {None} == set(MINIMAL)


@pytest.mark.parametrize("command", sorted(MINIMAL))
def test_global_flag_defaults(command: str) -> None:
    assert_parsed(MINIMAL[command], GLOBAL_DEFAULTS)


@pytest.mark.parametrize("command", sorted(MINIMAL))
@pytest.mark.parametrize(("flag", "expected"), GLOBAL_FLAGS)
def test_global_flag_before_subcommand(
    command: str, flag: list[str], expected: dict[str, Any]
) -> None:
    assert_parsed([*flag, *MINIMAL[command]], expected)


@pytest.mark.parametrize("command", sorted(MINIMAL))
@pytest.mark.parametrize(("flag", "expected"), GLOBAL_FLAGS)
def test_global_flag_after_subcommand(
    command: str, flag: list[str], expected: dict[str, Any]
) -> None:
    assert_parsed([*MINIMAL[command], *flag], expected)


@pytest.mark.parametrize(("flag", "expected"), GLOBAL_FLAGS)
def test_global_flag_between_label_and_its_subcommand(
    flag: list[str], expected: dict[str, Any]
) -> None:
    assert_parsed(["label", *flag, "add", ID, "k=v"], expected)


def test_global_flags_combine_before_and_after() -> None:
    assert_parsed(
        ["--json", "--socket", "a", "show", ID, "--config", "c.yaml", "--verbose"],
        {"json": True, "socket": "a", "config": "c.yaml", "verbose": True},
    )


@pytest.mark.parametrize(("flag", "_expected"), GLOBAL_FLAGS)
def test_global_flags_after_double_dash_are_open_argv(
    flag: list[str], _expected: dict[str, Any]
) -> None:
    assert_parsed(["open", "--", "cmd", *flag], {"argv": ["cmd", *flag], **GLOBAL_DEFAULTS})


@pytest.mark.parametrize(("flag", "_expected"), GLOBAL_FLAGS)
def test_global_flags_after_double_dash_are_positionals(
    flag: list[str], _expected: dict[str, Any]
) -> None:
    assert_parsed(["rename", ID, "--", flag[0]], {"name": flag[0], **GLOBAL_DEFAULTS})


def test_version_after_double_dash_is_argv() -> None:
    assert_parsed(["open", "--", "tool", "--version"], {"argv": ["tool", "--version"]})


def test_double_dash_before_positionals() -> None:
    assert_parsed(["rename", "--", ID, "-name"], {"id": ID, "name": "-name"})
    assert_parsed(["rename", ID, "--", "-name"], {"id": ID, "name": "-name"})
    assert_parsed(["mark", ID, "--", "-m"], {"marker": "-m"})


def test_open_argv_keeps_later_double_dashes() -> None:
    assert_parsed(
        ["open", "--name", "x", "--", "git", "diff", "--", "file", "--json"],
        {"argv": ["git", "diff", "--", "file", "--json"], "json": False},
    )


def test_open_argv_may_start_with_dashes() -> None:
    assert_parsed(["open", "--", "--name", "z"], {"argv": ["--name", "z"], "name": None})


def test_option_values_may_start_with_dash_in_equals_form() -> None:
    assert_parsed(["open", "--name=-x"], {"name": "-x"})


# --- usage errors -------------------------------------------------------------------------------

USAGE_ERRORS: list[list[str]] = [
    [],
    ["frobnicate"],
    ["label"],
    ["label", "rename", ID],
    ["show"],
    ["show", ID, "extra"],
    ["exists"],
    ["rename", ID],
    ["mark", ID],
    ["unmark"],
    ["focus"],
    ["close"],
    ["label", "add", ID],
    ["label", "remove", ID],
    ["list", "extra"],
    ["placements", "extra"],
    ["open", "--target", "tab"],
    ["open", "--keep-on-exit", "sometimes"],
    ["open", "--name"],
    ["open", "--no-such-flag"],
    ["open", "cmd"],
    ["open", "cmd", "--", "arg"],
    ["open", "--"],
    ["open", "--name", "x", "--"],
    ["show", ID, "--dry-run"],
    ["exists", ID, "--dry-run"],
    ["list", "--dry-run"],
    ["placements", "--dry-run"],
    ["show", ID, "--force"],
    ["focus", ID, "--force"],
    ["--verbose", "--quiet", "list"],
    ["list", "--verbose", "--quiet"],
    ["--verbose", "show", ID, "--quiet"],
    ["close", ID, "--grace", "5s"],
    ["close", ID, "--grace", "5s", "--dry-run"],
    ["--jso", "list"],
    ["list", "--sess", "s"],
    ["--socket"],
]


@pytest.mark.parametrize("argv", USAGE_ERRORS)
def test_usage_error_exits_2_with_message_on_stderr(argv: list[str]) -> None:
    run = Run(argv)
    assert run.code == 2
    assert run.stdout == ""
    assert run.stderr.startswith("termctl: ")
    assert run.gateway.calls == []


@pytest.mark.parametrize("argv", USAGE_ERRORS)
def test_usage_error_with_json_prints_envelope(argv: list[str]) -> None:
    run = Run(["--json", *argv])
    assert run.code == 2
    envelope = run.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert run.stderr.startswith("termctl: ")


def test_json_after_subcommand_selects_envelope_on_usage_error() -> None:
    run = Run(["close", ID, "--grace", "5s", "--json"])
    assert run.code == 2
    validate(run.json(), "termctl/cli/error.json")


def test_json_after_double_dash_does_not_select_envelope() -> None:
    run = Run(["open", "--"])
    assert run.code == 2
    assert run.stdout == ""
    run = Run(["rename", ID, "--", "--json", "extra"])
    assert run.code == 2
    assert run.stdout == ""


def test_verbose_with_quiet_message() -> None:
    run = Run(["--verbose", "--quiet", "list"])
    assert run.stderr == "termctl: --verbose and --quiet cannot be combined\n"


@pytest.mark.parametrize(
    ("argv", "golden"),
    [
        (["--json", "--verbose", "--quiet", "list"], "error-verbose-quiet.json"),
        (["--json", "close", ID, "--grace", "5s"], "error-grace-without-force.json"),
        (["--json", "open", "--"], "error-open-empty-argv.json"),
        (["--json", "open", "cmd"], "error-open-argv-without-double-dash.json"),
    ],
)
def test_usage_error_golden(argv: list[str], golden: str) -> None:
    run = Run(argv)
    expected = json.loads((GOLDEN / golden).read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert run.code == 2
    assert run.json() == expected


def test_grace_with_force_is_accepted() -> None:
    assert_parsed(["close", ID, "--force", "--grace", "5s"], {"force": True, "grace": "5s"})


def test_parse_args_raises_usage_error() -> None:
    with pytest.raises(UsageError):
        parse_args(["show"])


# --- --version ----------------------------------------------------------------------------------


def test_version_matches_package_metadata() -> None:
    assert __version__ == version("termctl")


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["--json", "--version"],
        ["show", "--version"],
        ["label", "add", "--version"],
        ["--version", "--verbose", "--quiet"],
    ],
)
def test_version_prints_version_and_exits_zero(argv: list[str]) -> None:
    run = Run(argv)
    assert run.code == 0
    assert run.stdout == f"termctl {__version__}\n"
    assert run.stderr == ""
    assert run.gateway.calls == []


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert Run(["--help"]).code == 0
    assert "usage: termctl" in capsys.readouterr().out


# --- check order --------------------------------------------------------------------------------

TMUX_COMMANDS_WITH_ID = [
    "show",
    "exists",
    "rename",
    "mark",
    "unmark",
    "focus",
    "close",
    "label add",
    "label remove",
]


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every check step and command handler with a recorder."""
    seen: list[str] = []

    def recorder(step: str):
        def record(args: Any, ctx: Context) -> None:
            seen.append(step)

        return record

    for step in ("load_config", "resolve_server", "check_tmux", "resolve_id"):
        monkeypatch.setattr(cli, step, recorder(step))

    def handler(args: Any, ctx: Context) -> Result:
        seen.append(f"run {args.command}")
        return Result(data={"ok": True}, human="ok")

    monkeypatch.setattr(cli, "run_command", handler)
    return seen


@pytest.mark.parametrize("command", TMUX_COMMANDS_WITH_ID)
def test_check_order_for_commands_with_an_id(command: str, steps: list[str]) -> None:
    assert Run(MINIMAL[command]).code == 0
    assert steps == ["load_config", "resolve_server", "check_tmux", "resolve_id", f"run {command}"]


@pytest.mark.parametrize("command", ["open", "list"])
def test_check_order_for_tmux_commands_without_an_id(command: str, steps: list[str]) -> None:
    assert Run(MINIMAL[command]).code == 0
    assert steps == ["load_config", "resolve_server", "check_tmux", f"run {command}"]


def test_placements_needs_no_tmux(steps: list[str]) -> None:
    assert Run(["placements"]).code == 0
    assert steps == ["load_config", "run placements"]


def test_usage_errors_stop_before_config(steps: list[str]) -> None:
    assert Run(["close", ID, "--grace", "1s"]).code == 2
    assert steps == []


def test_failed_step_stops_later_steps(monkeypatch: pytest.MonkeyPatch, steps: list[str]) -> None:
    def no_tmux(args: Any, ctx: Context) -> None:
        steps.append("check_tmux")
        raise TermctlError("tmux_missing", "tmux not found")

    monkeypatch.setattr(cli, "check_tmux", no_tmux)
    run = Run(["--json", "show", ID])
    assert run.code == 12
    assert steps == ["load_config", "resolve_server", "check_tmux"]
    assert run.json() == {"error": {"code": "tmux_missing", "message": "tmux not found"}}


# --- output -------------------------------------------------------------------------------------


def test_human_output_goes_to_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_command", lambda args, ctx: Result({"id": ID}, f"{ID} running"))
    run = Run(["placements"])
    assert (run.code, run.stdout, run.stderr) == (0, f"{ID} running\n", "")


def test_empty_human_output_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_command", lambda args, ctx: Result({"exists": True}, ""))
    assert Run(["placements"]).stdout == ""


def test_json_output_is_one_line_of_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_command", lambda args, ctx: Result({"id": ID, "n": 1}, "human"))
    run = Run(["placements", "--json"])
    assert run.stdout == '{"id": "tc-abcdefgh", "n": 1}\n'


def test_json_output_escapes_control_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_command", lambda args, ctx: Result({"s": "a\x1b\x7fb"}, ""))
    run = Run(["placements", "--json"])
    assert "\x1b" not in run.stdout and "\x7f" not in run.stdout
    assert run.json() == {"s": "a\x1b\x7fb"}


def test_result_exit_code_without_error_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "run_command", lambda args, ctx: Result({"exists": False}, "", exit_code=10)
    )
    run = Run(["--json", "placements"])
    assert (run.code, run.json()) == (10, {"exists": False})


def test_command_error_in_human_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(args: Any, ctx: Context) -> Result:
        raise TermctlError("refused", "pane is running", reason="running")

    monkeypatch.setattr(cli, "run_command", fail)
    run = Run(["placements"])
    assert (run.code, run.stdout, run.stderr) == (20, "", "termctl: pane is running\n")


def test_command_error_in_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(args: Any, ctx: Context) -> Result:
        raise TermctlError("refused", "pane is running", reason="running")

    monkeypatch.setattr(cli, "run_command", fail)
    run = Run(["--json", "placements"])
    assert run.code == 20
    assert run.json() == {
        "error": {"code": "refused", "message": "pane is running", "reason": "running"}
    }
    assert run.stderr == "termctl: pane is running\n"


def test_unexpected_exception_is_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(args: Any, ctx: Context) -> Result:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_command", crash)
    run = Run(["--json", "placements"])
    assert run.code == 1
    envelope = run.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"] == {
        "code": "internal",
        "message": "internal error: RuntimeError: boom",
    }


def test_quiet_suppresses_warnings_but_not_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def warn_then_fail(args: Any, ctx: Context) -> Result:
        ctx.out.warn("careful")
        raise TermctlError("not_found", "gone")

    monkeypatch.setattr(cli, "run_command", warn_then_fail)
    assert Run(["placements"]).stderr == "termctl: warning: careful\ntermctl: gone\n"
    assert Run(["placements", "--quiet"]).stderr == "termctl: gone\n"


def test_verbose_enables_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    def trace(args: Any, ctx: Context) -> Result:
        ctx.out.trace("tmux -V")
        return Result({}, "")

    monkeypatch.setattr(cli, "run_command", trace)
    assert Run(["placements"]).stderr == ""
    assert Run(["placements", "--verbose"]).stderr == "termctl: tmux -V\n"


def test_context_carries_injected_gateway_and_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Context] = []

    def capture(args: Any, ctx: Context) -> Result:
        seen.append(ctx)
        return Result({}, "")

    monkeypatch.setattr(cli, "run_command", capture)
    gateway = FakeGateway()
    stdout, stderr = io.StringIO(), io.StringIO()
    clock = FakeClock()
    main(
        ["placements"],
        gateway=gateway,
        clock=clock,
        environ={"HOME": "/h"},
        stdout=stdout,
        stderr=stderr,
    )
    assert seen[0].gateway is gateway
    assert seen[0].clock is clock
    assert seen[0].environ == {"HOME": "/h"}


# --- unimplemented commands ---------------------------------------------------------------------


@pytest.mark.parametrize("command", sorted(MINIMAL))
def test_unimplemented_command_is_internal_error(command: str) -> None:
    run = Run(["--json", *MINIMAL[command]])
    assert run.code == 1
    envelope = run.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"] == {
        "code": "internal",
        "message": f"termctl {command} is not implemented yet",
    }
    assert run.gateway.calls == []


def test_unimplemented_command_golden() -> None:
    expected = json.loads((GOLDEN / "error-not-implemented.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert Run(["--json", "show", ID]).json() == expected
