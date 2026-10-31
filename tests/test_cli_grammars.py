"""The grammars wired into step 1 of the check order: exit 2 before config or tmux."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from termctl import cli
from termctl.cli import Context, Result, main
from termctl.hooks import Hook

from .fakes import FakeClock, FakeGateway
from .schemas import validate

GOLDEN = Path(__file__).parent / "golden"
ID = "tc-abcdefgh"


DIRS = {"/", "/a b/#(x)", "/srv", "/home/u/src"}


def run(argv: list[str], gateway: FakeGateway | None = None) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        argv,
        gateway=gateway or FakeGateway(dirs=DIRS),
        clock=FakeClock(),
        environ={},
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture
def later_steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record whether any step after the grammar checks ran."""
    seen: list[str] = []

    def recorder(step: str):
        def record(args: Any, ctx: Context) -> None:
            seen.append(step)

        return record

    for step in ("load_config", "resolve_server", "check_tmux", "resolve_id"):
        monkeypatch.setattr(cli, step, recorder(step))

    def handler(args: Any, ctx: Context) -> Result:
        seen.append("run_command")
        return Result(data={}, human="")

    monkeypatch.setattr(cli, "run_command", handler)
    return seen


BAD: list[list[str]] = [
    ["--socket", ".", "list"],
    ["--socket", "..", "list"],
    ["--socket", "/", "list"],
    ["--socket", "/a/..", "list"],
    ["--socket", "rel/.", "list"],
    ["--socket", "/a,b/s", "list"],
    ["--socket", "/" + "x" * 100, "list"],
    ["list", "--socket", "/tmp/x/"],
    ["list", "--socket", "/tmp/x;"],
    ["list", "--socket", "/tmp/a\nb"],
    ["list", "--socket", "a b"],
    ["placements", "--socket", "$(id)"],
    ["show", "abc"],
    ["show", "tc-abcdefghi"],
    ["show", "abcd1"],
    ["exists", "ab\x1bcd"],
    ["rename", "", "n"],
    ["unmark", "%1"],
    ["focus", "x" * 5000],
    ["label", "add", "tc-ab", "k=v"],
    ["close", "abcd", "--force"],
    ["close", "ABCD", "--force", "--grace", "1s"],
    ["open", "--name", ""],
    ["open", "--name", "x" * 65],
    ["open", "--name", "a#[fg=red]"],
    ["open", "--name", "a\nb"],
    ["open", "--name", "a\x9bb"],
    ["open", "--cwd", "/a\tb"],
    ["open", "--cwd", "/a#[b"],
    ["open", "--cwd", ""],
    ["open", "--cwd", "/missing"],
    ["open", "--cwd", "missing"],
    ["open", "--env", "A"],
    ["open", "--env", "1A=x"],
    ["open", "--env", "A=1", "--env", "A=2"],
    ["open", *[arg for i in range(33) for arg in ("--env", f"V{i}=x")]],
    ["open", "--label", "k"],
    ["open", "--label", "K=v"],
    ["open", "--label", "k=a b"],
    ["open", *[arg for i in range(33) for arg in ("--label", f"k{i}=v")]],
    ["open", "--placement", "Work"],
    ["open", "--session", "a.b"],
    ["open", "--session", ""],
    ["open", "--actor", "root"],
    ["open", "--actor", "human:"],
    ["open", "--decision-id", "a b"],
    ["open", *[arg for _ in range(33) for arg in ("--decision-id", "d")]],
    ["list", "--label", "=v"],
    ["list", "--session", ""],
    ["rename", ID, "a#[b"],
    ["rename", ID, "--", ""],
    ["mark", ID, "Attention"],
    ["mark", ID, "--", "-m"],
    ["focus", ID, "--client", "a b"],
    ["focus", ID, "--client", "/dev/pts/1;"],
    ["close", ID, "--force", "--grace", "11m"],
    ["close", ID, "--force", "--grace", "01s"],
    ["close", ID, "--actor", "auto:"],
    ["close", ID, "--decision-id", "x;"],
    ["label", "add", ID, "k"],
    ["label", "remove", ID, "k=v", "bad key=v"],
    ["placements", "--label", "k=é"],
    ["open", "--hook", "pre-close=x"],
    ["open", "--hook", "pre-open"],
    ["open", "--hook", "pre-open="],
    ["open", "--hook", "on-open=x"],
    ["open", "--hook", "pre-open\x1b[2J=x"],
    ["open", "--hook", "pre-open=x\udcff"],
    ["close", ID, "--hook", "post-open=x"],
    ["open", "--override-escalation"],
    ["open", "--override-escalation", "--actor", "auto:policy"],
    ["close", ID, "--override-escalation"],
    ["close", ID, "--override-escalation", "--actor", "auto:human"],
    ["close", ID, "--override-escalation", "--dry-run"],
]


@pytest.mark.parametrize("argv", BAD)
def test_grammar_violation_exits_2_before_any_later_step(
    argv: list[str], later_steps: list[str]
) -> None:
    code, stdout, stderr = run(["--json", *argv])
    assert code == 2
    envelope = json.loads(stdout)
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert later_steps == []
    # Hostile input never reaches the terminal raw.
    assert "\x1b" not in stderr and "\x9b" not in stderr and "\n" not in stderr[:-1]


GOOD: list[list[str]] = [
    ["--socket", "work", "list"],
    ["--socket", "rel/sock", "list"],
    ["--socket", "/tmp/tmux-1000/default", "list"],
    ["show", "ABCD"],
    ["show", "TC-ABCDEFGH"],
    ["close", "ABCDEFGH", "--force", "--grace", "10m"],
    ["close", "abcd"],
    ["open", "--name", "PR#42 review", "--cwd", "/a b/#(x)", "--env", "A=$(id);"],
    ["open", *[arg for i in range(32) for arg in ("--label", f"k{i}=v")], "--label", "k0=v"],
    ["open", "--actor", "human:me", "--decision-id", "a:b/c", "--session", "s_1"],
    ["list", "--session", "any thing: 1.2"],
    ["list", *[arg for i in range(40) for arg in ("--label", f"k{i}=v")]],
    ["rename", ID, "--", "--json"],
    ["mark", ID, "attention"],
    ["focus", ID, "--client", "/dev/pts/3"],
    ["label", "add", ID, "k=a=b", "x="],
    # The 32-label limit of label add is checked after id resolution, not in step 1.
    ["label", "add", ID, *[f"k{i}=v" for i in range(33)]],
    ["placements", "--label", "team=a#b"],
    ["open", "--hook", "pre-open=gate check --x=1", "--hook", "post-open=notify; true"],
    ["close", ID, "--hook", "pre-close=x", "--hook", "post-close=y"],
    ["open", "--override-escalation", "--actor", "human:me"],
    ["close", ID, "--override-escalation", "--actor", "human:me"],
]


@pytest.mark.parametrize("argv", GOOD)
def test_valid_input_passes_to_the_later_steps(argv: list[str], later_steps: list[str]) -> None:
    code, _, stderr = run(argv)
    assert (code, stderr) == (0, "")
    assert "run_command" in later_steps


def test_invalid_id_golden() -> None:
    code, stdout, _ = run(["--json", "show", "tc-abc"])
    expected = json.loads((GOLDEN / "error-invalid-id.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert (code, json.loads(stdout)) == (2, expected)


def test_force_prefix_golden() -> None:
    code, stdout, _ = run(["--json", "close", "abcd", "--force"])
    expected = json.loads((GOLDEN / "error-force-needs-full-id.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert (code, json.loads(stdout)) == (2, expected)


def test_env_value_never_appears_in_the_error() -> None:
    code, stdout, stderr = run(["--json", "open", "--env", "TOKEN=s3cret", "--env", "TOKEN=x"])
    assert code == 2
    assert "s3cret" not in stdout + stderr


def test_open_checks_the_cwd_in_step_1(later_steps: list[str]) -> None:
    gateway = FakeGateway(cwd="/home/u", dirs={"/home/u"})
    code, stdout, _ = run(["--json", "open", "--cwd", "src"], gateway)
    assert code == 2
    assert json.loads(stdout)["error"]["code"] == "usage"
    assert gateway.dir_checks == ["/home/u/src"]
    assert later_steps == []


def test_open_resolves_a_relative_cwd(later_steps: list[str]) -> None:
    gateway = FakeGateway(cwd="/home/u", dirs=DIRS)
    assert run(["open", "--cwd", "src"], gateway)[0] == 0
    assert gateway.dir_checks == ["/home/u/src"]


def test_open_absolute_cwd_works_without_a_readable_current_dir(later_steps: list[str]) -> None:
    gateway = FakeGateway(cwd=None, dirs=DIRS)
    assert run(["open", "--cwd", "/srv"], gateway) == (0, "", "")
    assert "run_command" in later_steps


def test_open_without_cwd_needs_a_readable_current_dir(later_steps: list[str]) -> None:
    code, _, stderr = run(["open"], FakeGateway(cwd=None, dirs=DIRS))
    assert code == 2
    assert "current directory" in stderr
    assert later_steps == []


@pytest.mark.parametrize("argv", [["open"], ["close", ID]])
def test_override_escalation_is_accepted_from_a_tty(
    argv: list[str], later_steps: list[str]
) -> None:
    gateway = FakeGateway(dirs=DIRS, tty=True)
    assert run([*argv, "--override-escalation"], gateway) == (0, "", "")
    assert "run_command" in later_steps


def test_flag_hooks_are_parsed_in_step_1(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def handler(args: Any, ctx: Context) -> Result:
        seen.append(args.flag_hooks)
        return Result(data={}, human="")

    for step in ("load_config", "resolve_server", "check_tmux", "resolve_id"):
        monkeypatch.setattr(cli, step, lambda args, ctx: None)
    monkeypatch.setattr(cli, "run_command", handler)
    assert run(["open", "--hook", "post-open=a", "--hook", "pre-open=b=c"])[0] == 0
    assert seen == [[Hook("post-open", "a", 30), Hook("pre-open", "b=c", 30)]]


@pytest.mark.parametrize(
    ("argv", "golden"),
    [
        (["open", "--override-escalation"], "error-override-escalation-gate.json"),
        (["close", ID, "--hook", "pre-open=x"], "error-hook-name.json"),
    ],
)
def test_hook_flag_goldens(argv: list[str], golden: str) -> None:
    code, stdout, _ = run(["--json", *argv])
    expected = json.loads((GOLDEN / golden).read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    assert (code, json.loads(stdout)) == (2, expected)
