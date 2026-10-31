"""`rename` through the whole check order against a fake tmux (interface.md `rename`, "Names",
"Check order", "Panes that vanish", "Audit and log"). Real-tmux tests are in
test_rename_integration.py."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from termctl import cli, lock
from termctl import tmux_backend as tb
from termctl.cli import main
from termctl.gateway import Terminated
from termctl.tmux_backend import Invocation, command

from .fakes import TMUX, UID, FakeClock, FakeGateway, completed, is_list_panes, pane_row
from .schemas import validate
from .test_cli import NO_CONFIG

GOLDEN = Path(__file__).parent / "golden"
SOCK = f"/tmp/tmux-{UID}/default"
ID = "tc-abcdefgh"
PANE = "%3"
NOMATCH = completed(tb.NOMATCH + "\n")


def _argv(*commands: tb.Command) -> tuple[str, ...]:
    return tuple(Invocation.of(*commands).argv(TMUX, SOCK, utf8=True))


def _set_name(name: str) -> tb.Command:
    target = tb.pane(PANE)
    return command("set").flag("-p").target(target).arg("@termctl_name").arg(name).build()


def combined(name: str, display_name: str | None = None) -> tuple[str, ...]:
    """The guarded option write and window rename of a rename to ``name``."""
    rename = command("rename-window").target(tb.pane(PANE)).name(display_name or name).build()
    return _argv(
        tb.guarded(tb.pane(PANE), tb.owned_single_pane_window(ID), _set_name(name), rename)
    )


def options_only(name: str) -> tuple[str, ...]:
    return _argv(tb.guarded(tb.pane(PANE), tb.owned(ID), _set_name(name)))


def lengths(*names: str) -> tuple[str, ...]:
    probe = command("display").flag("-p").target(tb.pane(PANE))
    return _argv(probe.format_arg(tb.length_probe(*names)).build())


def measured(*values: int, tab: bool = False) -> subprocess.CompletedProcess[str]:
    """The length probe's answer for the pane of ``pane_row()``: its identity, ``values``
    and, last, whether the window name holds a tab or newline."""
    shown = " ".join(str(v) for v in values)
    return completed(f"{PANE} @2 {PANE} {ID} {shown} {int(tab)}\n")


class Server(FakeGateway):
    """A fake tmux whose ``list-panes`` rows change when a scripted write runs: ``writes``
    maps the argv of a write to its result and the row values it sets (on a 0 exit with no
    termctl-nomatch)."""

    def __init__(
        self,
        rows: Sequence[Mapping[str, str]],
        writes: Mapping[tuple[str, ...], tuple[subprocess.CompletedProcess[str], dict[str, str]]]
        | None = None,
        responses: Mapping[tuple[str, ...], Any] | None = None,
    ) -> None:
        self.writes = dict(writes or {})
        scripted = {argv: result for argv, (result, _) in self.writes.items()}
        super().__init__({**scripted, **(responses or {})}, panes=rows)

    def _respond(self, argv: list[str], env: Mapping[str, str] | None) -> Any:
        response = super()._respond(argv, env)
        write = self.writes.get(tuple(argv))
        if write is not None and response.returncode == 0 and tb.NOMATCH not in response.stdout:
            assert self.panes is not None
            for row in self.panes:
                if row["pane_id"] == PANE:
                    row.update(write[1])
        return response


class Outcome:
    def __init__(self, code: int, stdout: str, stderr: str, state: Path) -> None:
        self.code, self.stdout, self.stderr, self.state = code, stdout, stderr, state

    def json(self) -> Any:
        return json.loads(self.stdout)

    def log(self) -> list[dict[str, Any]]:
        path = self.state / "termctl" / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def run(
    argv: list[str],
    gateway: FakeGateway,
    state: Path,
    environ: Mapping[str, str] | None = None,
) -> Outcome:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        argv,
        gateway=gateway,
        clock=FakeClock(),
        environ={**NO_CONFIG, "PATH": "/usr/bin", "XDG_STATE_HOME": str(state), **(environ or {})},
        stdout=stdout,
        stderr=stderr,
    )
    return Outcome(code, stdout.getvalue(), stderr.getvalue(), state)


def renaming(name: str = "new", **row: str) -> Server:
    """A server with one managed pane named KEY-12 that ``rename`` can rename to ``name``."""
    return Server(
        [pane_row(overrides=row)],
        writes={combined(name): (completed(), {"@termctl_name": name, "window_name": name})},
    )


def writes(gateway: FakeGateway) -> list[list[str]]:
    return [call for call in gateway.calls if "if-shell" in call]


# --- a real rename --------------------------------------------------------------------------------


def test_rename_golden(state: Path) -> None:
    expected = json.loads((GOLDEN / "rename.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/rename.json")
    result = run(["--json", "rename", "abcd", "new"], renaming(), state)
    assert (result.code, result.stderr) == (0, "")
    assert result.json() == expected


def test_rename_reads_writes_and_reads_back_under_the_lock(state: Path) -> None:
    gateway = renaming()
    result = run(["rename", ID, "new"], gateway, state)
    assert (result.code, result.stdout, result.stderr) == (0, "new\n", "")
    tmux_calls = [c for c in gateway.calls if c != [TMUX, "-V"]]
    # Step 4, then under the lock: the read again, the guarded write, the read back.
    assert [is_list_panes(c) for c in tmux_calls] == [True, True, False, True]
    assert tmux_calls[2] == list(combined("new"))
    assert not lock.held()
    assert (state / "termctl" / "locks" / lock.lock_name(SOCK)).exists()


def test_rename_keeps_a_set_marker_in_the_displayed_name(state: Path) -> None:
    marked = {"@termctl_marker": "attention", "@termctl_marker_text": "! ",
              "window_name": "! KEY-12"}  # fmt: skip
    gateway = Server(
        [pane_row(overrides=marked)],
        writes={
            combined("new", "! new"): (
                completed(),
                {"@termctl_name": "new", "window_name": "! new"},
            )
        },  # fmt: skip
    )
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 0
    out = result.json()
    assert (out["name"], out["display_name"], out["marker"]) == ("new", "! new", "attention")
    assert (out["changed"], out["display_updated"]) == (True, True)


def test_rename_with_marker_display_option_renames_to_the_name(state: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("marker_display: option\n")
    marked = {"@termctl_marker": "attention", "@termctl_marker_text": "! "}
    gateway = Server(
        [pane_row(overrides=marked)],
        writes={combined("new"): (completed(), {"@termctl_name": "new", "window_name": "new"})},
    )
    result = run(["--json", "--config", str(config), "rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert result.json()["display_name"] == "new"


@pytest.mark.parametrize(
    "row",
    [{"window_panes": "2"}, {"window_id": "@7"}],
    ids=["split window", "moved pane"],
)
def test_rename_of_a_window_it_does_not_own_alone_sets_the_option_only(
    state: Path, row: dict[str, str]
) -> None:
    gateway = Server(
        [pane_row(overrides={**row, "window_name": "user's"})],
        writes={options_only("new"): (completed(), {"@termctl_name": "new"})},
    )
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["name"], out["display_name"]) == ("new", "user's")
    assert (out["changed"], out["display_updated"]) == (True, False)
    assert writes(gateway) == [list(options_only("new"))]
    assert result.log()[0]["result"] == "ok"


def test_a_window_split_between_the_read_and_the_write_is_not_renamed(state: Path) -> None:
    gateway = Server(
        [pane_row()],
        writes={
            combined("new"): (NOMATCH, {}),
            options_only("new"): (completed(), {"@termctl_name": "new"}),
        },
    )
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert (result.json()["changed"], result.json()["display_updated"]) == (True, False)
    assert writes(gateway) == [list(combined("new")), list(options_only("new"))]


@pytest.mark.parametrize(
    "row",
    [
        {"pane_dead": "1", "pane_dead_status": "0"},
        {"pane_dead": "1", "pane_dead_signal": "9"},
        {"pane_dead": "1"},
    ],
)
def test_a_dead_pane_is_renamed(state: Path, row: dict[str, str]) -> None:
    result = run(["--json", "rename", ID, "new"], renaming(**row), state)
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["state"], out["display_name"], out["changed"]) == ("dead", "new", True)


@pytest.mark.parametrize(
    "name", ["PR#42 review", "#{pane_id}", "##", "a;", "-x", "a\\b", "$HOME", "é中\U0001f514"]
)
def test_names_reach_tmux_through_the_argument_builder(state: Path, name: str) -> None:
    gateway = renaming(name)
    result = run(["rename", ID, "--", name], gateway, state)
    assert result.code == 0
    assert writes(gateway) == [list(combined(name))]


def test_rename_writes_an_ok_log_record(state: Path) -> None:
    result = run(["rename", ID, "new"], renaming(), state)
    assert result.code == 0
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert record == {
        "ts": "2026-01-01T00:00:00.000Z",
        "level": "info",
        "event": "rename",
        "id": ID,
        "socket_path": SOCK,
        "result": "ok",
        "exit_code": 0,
    }


# --- no-ops -------------------------------------------------------------------------------------


def test_renaming_to_the_current_name_is_a_no_op(state: Path) -> None:
    gateway = renaming()
    result = run(["--json", "rename", ID, "KEY-12"], gateway, state)
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (False, False, False)
    assert writes(gateway) == []
    assert result.log() == []


def test_a_no_op_with_a_doubled_backslash_window_name(state: Path) -> None:
    gateway = Server(
        [pane_row(overrides={"@termctl_name": "a\\b", "window_name": "a\\\\b"})],
        responses={lengths("@termctl_name", "window_name"): measured(3, 4)},
    )
    result = run(["--json", "rename", ID, "a\\b"], gateway, state)
    assert result.code == 0
    assert result.json()["changed"] is False
    assert writes(gateway) == []
    assert result.log() == []


def test_a_forged_control_byte_is_not_a_no_op(state: Path) -> None:
    # @termctl_name holds "n" ESC "x", which tmux prints as the text n\033x.
    printed = "n\\033x"
    gateway = Server(
        [pane_row(overrides={"@termctl_name": printed, "window_name": "n\\\\033x"})],
        writes={combined(printed): (completed(), {"window_name": "n\\\\033x"})},
        responses={lengths("@termctl_name", "window_name"): measured(3, 7)},
    )
    result = run(["--json", "rename", ID, printed], gateway, state)
    assert result.code == 0
    assert result.json()["changed"] is True
    assert writes(gateway) == [list(combined(printed))]
    assert result.log()[0]["result"] == "ok"


def test_a_stale_window_name_is_not_a_no_op(state: Path) -> None:
    gateway = Server(
        [pane_row(overrides={"window_name": "renamed by a program"})],
        writes={combined("KEY-12"): (completed(), {"window_name": "KEY-12"})},
    )
    result = run(["--json", "rename", ID, "KEY-12"], gateway, state)
    assert (result.code, result.json()["changed"], result.json()["display_updated"]) == (
        0,
        True,
        True,
    )


# --- panes that vanish or change --------------------------------------------------------------


def test_a_guard_mismatch_exits_10_and_changes_nothing(state: Path) -> None:
    # Spike F5: the server restarted and %3 is now another pane; neither guard matches.
    gateway = Server(
        [pane_row()],
        writes={combined("new"): (NOMATCH, {}), options_only("new"): (NOMATCH, {})},
    )
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 10
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "not_found"
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["result"], record["level"], record["exit_code"]) == ("error", "error", 10)
    assert (record["id"], record["socket_path"]) == (ID, SOCK)
    assert record["message"] == envelope["error"]["message"]


def test_a_pane_tmux_cannot_find_exits_10(state: Path) -> None:
    gone = completed(stderr=f"can't find pane: {PANE}\n", returncode=1)
    gateway = Server([pane_row()], writes={combined("new"): (gone, {})})
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")


def test_another_tmux_failure_exits_12(state: Path) -> None:
    failed = completed(stderr="server exited unexpectedly\n", returncode=1)
    gateway = Server([pane_row()], writes={combined("new"): (failed, {})})
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (12, "server_unavailable")
    assert result.log()[0]["details"] == {"socket_path": SOCK}


class Vanishing(Server):
    """Removes the pane after ``reads`` list-panes reads."""

    def __init__(self, rows: Sequence[Mapping[str, str]], reads: int) -> None:
        super().__init__(rows)
        self.reads = reads

    def _respond(self, argv: list[str], env: Mapping[str, str] | None) -> Any:
        if is_list_panes(argv):
            if self.reads == 0:
                self.panes = []
            self.reads -= 1
        return super()._respond(argv, env)


def test_a_pane_gone_before_the_lock_exits_10_without_a_write(state: Path) -> None:
    gateway = Vanishing([pane_row()], reads=1)
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    assert writes(gateway) == []
    assert result.log()[0]["exit_code"] == 10


def test_an_unknown_id_is_logged_without_an_id(state: Path) -> None:
    result = run(["--json", "rename", "tc-zzzzzzzz", "new"], renaming(), state)
    assert result.code == 10
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["id"], record["socket_path"], record["exit_code"]) == (None, SOCK, 10)


def test_an_ambiguous_id_exits_21_and_is_logged(state: Path) -> None:
    gateway = Server([pane_row(), pane_row(pane_id="%4")])
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 21
    assert writes(gateway) == []
    assert result.log()[0]["exit_code"] == 21


def test_no_server_exits_10_and_is_logged(state: Path) -> None:
    gateway = Server([], responses={})
    gateway.list_panes = completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (10, "no_server")
    assert result.log()[0]["exit_code"] == 10


# --- what is not logged, and log failures ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["rename", ID, "a#[b"],
        ["rename", "x", "n"],
        ["rename", ID],
        ["--socket", "/a,b", "rename", ID, "n"],
        ["--socket", "bad name", "rename", ID, "n"],
    ],
)
def test_usage_errors_are_not_logged(state: Path, argv: list[str]) -> None:
    result = run(argv, renaming(), state)
    assert result.code == 2
    assert result.log() == []


def test_a_server_resolution_error_is_logged_without_an_id_or_socket(state: Path) -> None:
    # Step 2: a TMUX whose first field is not an absolute socket path exits 2 (config_invalid).
    result = run(["rename", ID, "n"], renaming(), state, environ={"TMUX": "rel/sock,1,0"})
    assert result.code == 2
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["event"], record["result"], record["exit_code"]) == ("rename", "error", 2)
    assert (record["id"], record["socket_path"]) == (None, None)


def test_a_tmux_check_failure_is_logged_without_an_id(state: Path) -> None:
    gateway = FakeGateway({(TMUX, "-V"): completed("tmux 3.1c\n")})
    result = run(["rename", ID, "n"], gateway, state)
    assert result.code == 12
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["id"], record["socket_path"]) == (None, SOCK)
    assert record["details"]["tmux_version"] == "3.1"


def test_a_reader_warning_gets_a_warning_record(state: Path) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = Server(
        [pane_row(), bad],
        writes={combined("new"): (completed(), {"@termctl_name": "new", "window_name": "new"})},
    )
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert "ignoring managed pane %9" in result.stderr
    warning, done = result.log()
    for record in (warning, done):
        validate(record, "termctl/log.json")
    assert (warning["event"], warning["result"], warning["level"]) == ("warning", "warning", "warn")
    assert warning["id"] is None and "ignoring managed pane %9" in warning["message"]
    assert done["event"] == "rename"


def test_a_no_op_writes_no_warning_record(state: Path) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = Server([pane_row(), bad])
    gateway.responses[(TMUX, "-V")] = completed("tmux master\n")
    result = run(["--json", "rename", ID, "KEY-12"], gateway, state)
    assert (result.code, result.json()["changed"]) == (0, False)
    assert "ignoring managed pane %9" in result.stderr
    assert not (state / "termctl" / "log.jsonl").exists()


def test_warnings_are_logged_before_the_record_of_a_change(state: Path) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = Server(
        [pane_row(), bad],
        writes={combined("new"): (completed(), {"@termctl_name": "new", "window_name": "new"})},
    )
    gateway.responses[(TMUX, "-V")] = completed("tmux master\n")
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == 0
    records = result.log()
    assert [r["event"] for r in records] == ["warning", "warning", "rename"]
    assert "ignoring managed pane %9" in records[1]["message"]


def test_warnings_are_logged_before_the_record_of_a_failure(state: Path) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = Server([pane_row(), bad], writes={combined("new"): (NOMATCH, {})})
    gateway.responses[options_only("new")] = NOMATCH
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == 10
    assert [(r["event"], r["result"]) for r in result.log()] == [
        ("warning", "warning"),
        ("rename", "error"),
    ]


class Invalidating(Server):
    """Makes the pane invalid (a bad ``@termctl_keep``) after the step-4 read, as if someone
    changed it while termctl waited for the lock."""

    def _respond(self, argv: list[str], env: Mapping[str, str] | None) -> Any:
        response = super()._respond(argv, env)
        if is_list_panes(argv):
            assert self.panes is not None
            self.panes[0]["@termctl_keep"] = "sometimes"
        return response


def test_a_pane_turned_invalid_under_the_lock_is_gone_with_its_warning(state: Path) -> None:
    gateway = Invalidating([pane_row()])
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == 10
    assert result.stderr.count(f"ignoring managed pane {PANE}") == 1
    warning, error = result.log()
    assert (warning["event"], error["event"], error["exit_code"]) == ("warning", "rename", 10)
    assert f"ignoring managed pane {PANE}" in warning["message"]
    assert writes(gateway) == []


class Stopping(Server):
    """Raises ``stop`` right after the guarded write landed, as a signal would."""

    def __init__(self, stop: BaseException) -> None:
        super().__init__(
            [pane_row()],
            writes={combined("new"): (completed(), {"@termctl_name": "new", "window_name": "new"})},
        )
        self.stop = stop

    def _respond(self, argv: list[str], env: Mapping[str, str] | None) -> Any:
        response = super()._respond(argv, env)
        if tuple(argv) == combined("new"):
            raise self.stop
        return response


@pytest.mark.parametrize(
    ("stop", "code", "message"),
    [
        (KeyboardInterrupt(), 130, "interrupted"),
        (Terminated(15), 143, "terminated by signal 15"),
    ],
)
def test_a_signal_after_the_write_is_logged_with_its_exit_code(
    state: Path, stop: BaseException, code: int, message: str
) -> None:
    bad = pane_row(pane_id="%9", tc_id="tc-zzzzzzzz", overrides={"@termctl_keep": "sometimes"})
    gateway = Stopping(stop)
    assert gateway.panes is not None
    gateway.panes.append(bad)
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == code
    warning, record = result.log()
    validate(record, "termctl/log.json")
    assert warning["event"] == "warning"
    assert (record["event"], record["result"], record["exit_code"]) == ("rename", "error", code)
    assert (record["id"], record["message"]) == (ID, message)
    assert gateway.panes[0]["@termctl_name"] == "new"  # the change landed


def test_a_tmux_version_warning_gets_a_warning_record(state: Path) -> None:
    gateway = renaming()
    gateway.responses[(TMUX, "-V")] = completed("tmux master\n")
    result = run(["rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert [r["event"] for r in result.log()] == ["warning", "rename"]


def test_a_log_that_cannot_be_written_warns_and_keeps_the_exit_code(state: Path) -> None:
    (state / "termctl").mkdir(parents=True, mode=0o700)
    (state / "termctl" / "log.jsonl").mkdir()
    gateway = renaming()
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 0
    assert result.json()["changed"] is True
    assert "could not write the log record" in result.stderr
    assert "log.jsonl" in result.stderr


def test_a_log_failure_after_an_error_keeps_the_error(state: Path) -> None:
    (state / "termctl").mkdir(parents=True, mode=0o700)
    (state / "termctl" / "log.jsonl").mkdir()
    result = run(["--json", "rename", "tc-zzzzzzzz", "n"], renaming(), state)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")
    assert "could not write the log record" in result.stderr


def test_quiet_suppresses_the_log_failure_warning(state: Path) -> None:
    (state / "termctl").mkdir(parents=True, mode=0o700)
    (state / "termctl" / "log.jsonl").mkdir()
    result = run(["--quiet", "rename", ID, "new"], renaming(), state)
    assert (result.code, result.stderr) == (0, "")


def test_an_unusable_state_directory_exits_12_locked_before_any_write(state: Path) -> None:
    state.parent.mkdir(exist_ok=True)
    state.write_text("a file, not a directory")
    gateway = renaming()
    result = run(["--json", "rename", ID, "new"], gateway, state)
    assert result.code == 12
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "locked"
    assert envelope["error"]["details"]["cause"] == "unusable"
    assert writes(gateway) == []


def test_dry_run_is_not_implemented_yet_and_not_logged(state: Path) -> None:
    gateway = renaming()
    result = run(["--json", "rename", ID, "new", "--dry-run"], gateway, state)
    assert (result.code, result.json()["error"]["code"]) == (1, "internal")
    assert writes(gateway) == []
    assert result.log() == []
    assert not (state / "termctl" / "locks").exists()


def test_an_unexpected_failure_is_logged_as_internal(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "change_pane", broken)
    result = run(["--json", "rename", ID, "new"], renaming(), state)
    assert result.code == 1
    assert result.json()["error"] == {
        "code": "internal",
        "message": "internal error: RuntimeError: boom",
    }
    [record] = result.log()
    validate(record, "termctl/log.json")
    assert (record["result"], record["exit_code"], record["id"]) == ("error", 1, ID)
