"""`show`, `exists` and `list` through the whole check order, against a fake tmux
(interface.md `show`, `exists`, `list`, "Check order", "Identifiers", "Human output").
Real-tmux end-to-end tests are in test_cli_read_integration.py."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from termctl.gateway import FileInfo

from .fakes import (
    TMUX,
    UID,
    FakeGateway,
    completed,
    directory,
    is_list_panes,
    pane_row,
    safe_default_server,
)
from .schemas import validate
from .test_cli import ENVIRON, NO_CONFIG, Run

GOLDEN = Path(__file__).parent / "golden"
SOCK = f"/tmp/tmux-{UID}/default"
ID = "tc-abcdefgh"
OTHER = "tc-zzzzzzzz"

RUNNING = pane_row(
    overrides={"@termctl_marker": "attention", "@termctl_marker_text": "\U0001f514 "}
)
DEAD_STATUS = pane_row(overrides={"pane_dead": "1", "pane_dead_status": "3"})
DEAD_SIGNAL = pane_row(overrides={"pane_dead": "1", "pane_dead_signal": "9"})
# tmux 3.2 has no #{pane_dead_signal}: a pane killed by a signal reports neither field.
DEAD_NO_EXIT = pane_row(overrides={"pane_dead": "1"})


def golden(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def run(
    argv: list[str],
    rows: Sequence[Mapping[str, str]] | None = None,
    *,
    gateway: FakeGateway | None = None,
    environ: Mapping[str, str] | None = None,
) -> Run:
    return Run(
        argv,
        gateway or FakeGateway(panes=[RUNNING] if rows is None else rows),
        environ={**ENVIRON, **(environ or {})},
    )


def list_panes_calls(gateway: FakeGateway) -> list[list[str]]:
    return [call for call in gateway.calls if is_list_panes(call)]


def no_socket() -> FakeGateway:
    files = safe_default_server()
    del files[SOCK]
    return FakeGateway(files=files)


def no_parent() -> FakeGateway:
    return FakeGateway(files={"/": directory(), "/tmp": directory(0o1777)})


# --- show ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "name"),
    [
        (RUNNING, "show-running.json"),
        (DEAD_STATUS, "show-dead-exit-status.json"),
        (DEAD_SIGNAL, "show-dead-exit-signal.json"),
        (DEAD_NO_EXIT, "show-dead-no-exit.json"),
    ],
)
def test_show_golden(row: Mapping[str, str], name: str) -> None:
    expected = golden(name)
    validate(expected, "termctl/cli/show.json")
    result = run(["--json", "show", ID], [row])
    assert (result.code, result.json(), result.stderr) == (0, expected, "")


def test_show_runs_one_list_panes_on_the_resolved_socket() -> None:
    result = run(["show", ID])
    assert result.gateway.calls[0] == [TMUX, "-V"]
    (call,) = list_panes_calls(result.gateway)
    assert call[:6] == [TMUX, "-u", "-S", SOCK, "list-panes", "-a"]
    assert len(result.gateway.calls) == 2


def test_show_human_line() -> None:
    result = run(["show", "ABCD"])
    assert (result.code, result.stdout, result.stderr) == (
        0,
        "tc-abcdefgh running termctl KEY-12\n",
        "",
    )


def test_show_dead_human_line() -> None:
    assert run(["show", ID], [DEAD_STATUS]).stdout == "tc-abcdefgh dead termctl KEY-12\n"


def test_show_resolves_a_prefix_among_several_panes() -> None:
    rows = [pane_row("%1", OTHER), pane_row("%7", ID, {"window_name": "seven"})]
    result = run(["--json", "show", "abcd"], rows)
    assert (result.json()["id"], result.json()["pane_id"]) == (ID, "%7")


def test_show_of_an_unknown_id_is_not_found() -> None:
    result = run(["--json", "show", "qqqq"])
    assert result.code == 10
    validate(result.json(), "termctl/cli/error.json")
    assert result.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("gateway", [no_socket, no_parent], ids=["no-socket", "no-parent"])
def test_show_without_a_server_is_no_server_and_never_connects(gateway: Any) -> None:
    result = run(["--json", "show", ID], gateway=gateway())
    assert result.code == 10
    validate(result.json(), "termctl/cli/error.json")
    assert result.json()["error"] == {
        "code": "no_server",
        "message": f"no tmux server is running on '{SOCK}'",
        "details": {"socket_path": SOCK},
    }
    assert result.gateway.calls == [[TMUX, "-V"]]


@pytest.mark.parametrize(
    "stderr",
    [f"no server running on {SOCK}\n", f"error connecting to {SOCK} (No such file or directory)\n"],
)
def test_show_on_a_refused_connection_is_no_server(stderr: str) -> None:
    gateway = FakeGateway(list_panes=completed(stderr=stderr, returncode=1))
    result = run(["--json", "show", ID], gateway=gateway)
    assert (result.code, result.json()["error"]["code"]) == (10, "no_server")


def test_show_on_a_server_with_no_sessions_is_not_found() -> None:
    gateway = FakeGateway(list_panes=completed(stderr="no current target\n", returncode=1))
    result = run(["--json", "show", ID], gateway=gateway)
    assert (result.code, result.json()["error"]["code"]) == (10, "not_found")


@pytest.mark.parametrize("command", ["show", "exists"])
def test_two_panes_carrying_the_same_full_id_are_ambiguous(command: str) -> None:
    # Any process in a pane can copy another managed pane's options onto its own pane.
    rows = [pane_row("%10", ID), pane_row("%9", ID, {"session_name": "copy"})]
    result = run(["--json", command, ID], rows)
    assert result.code == 21
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "ambiguous"
    assert envelope["error"]["candidates"] == [
        {"id": ID, "pane_id": "%9", "session": "copy"},
        {"id": ID, "pane_id": "%10", "session": "termctl"},
    ]
    assert "exists" not in envelope


def test_show_reports_a_forged_but_valid_name_as_read() -> None:
    # Anything with socket access can write the options; a grammar-valid value is reported.
    row = pane_row(overrides={"@termctl_name": "approved-by-admin", "@termctl_pid": "1"})
    obj = run(["--json", "show", ID], [row]).json()
    assert (obj["name"], obj["pid"]) == ("approved-by-admin", 1)


# --- exists -------------------------------------------------------------------------------------


def test_exists_found() -> None:
    result = run(["--json", "exists", "ABCD"])
    assert result.code == 0
    validate(result.json(), "termctl/cli/exists.json")
    assert result.json() == {"input": "ABCD", "id": ID, "exists": True, "socket_path": SOCK}
    assert result.stderr == ""


def test_exists_dead_pane_exists() -> None:
    assert run(["exists", ID], [DEAD_NO_EXIT]).code == 0


def test_exists_not_found_golden() -> None:
    expected = golden("exists-not-found.json")
    validate(expected, "termctl/cli/exists.json")
    result = run(["--json", "exists", "tc-qqqqqqqq"])
    assert (result.code, result.json(), result.stderr) == (10, expected, "")


@pytest.mark.parametrize("gateway", [no_socket, no_parent], ids=["no-socket", "no-parent"])
def test_exists_without_a_server_prints_its_output_and_never_connects(gateway: Any) -> None:
    result = run(["--json", "exists", ID], gateway=gateway())
    assert result.code == 10
    validate(result.json(), "termctl/cli/exists.json")
    assert result.json() == {"input": ID, "id": None, "exists": False, "socket_path": SOCK}
    assert result.gateway.calls == [[TMUX, "-V"]]
    assert result.stderr == ""


def test_exists_on_a_refused_connection_prints_its_output() -> None:
    gateway = FakeGateway(
        list_panes=completed(stderr=f"no server running on {SOCK}\n", returncode=1)
    )
    result = run(["--json", "exists", ID], gateway=gateway)
    assert (result.code, result.json()["exists"]) == (10, False)


def test_exists_human_output_is_empty() -> None:
    assert (run(["exists", ID]).code, run(["exists", ID]).stdout) == (0, "")
    missing = run(["exists", "qqqq"])
    assert (missing.code, missing.stdout, missing.stderr) == (10, "", "")


# --- list ---------------------------------------------------------------------------------------


LISTED = [
    pane_row("%1", "tc-cccccccc", {"@termctl_created": "2026-10-30T12:00:01.000Z"}),
    pane_row("%2", "tc-bbbbbbbb", {"@termctl_labels": "kind=review repo=x"}),
    pane_row(
        "%3",
        "tc-aaaaaaaa",
        {"pane_dead": "1", "pane_dead_status": "0", "session_name": "work", "window_name": "w"},
    ),
]


def test_list_golden() -> None:
    expected = golden("list.json")
    validate(expected, "termctl/cli/list.json")
    result = run(["--json", "list"], LISTED)
    assert (result.code, result.json(), result.stderr) == (0, expected, "")


def test_list_human_lines_in_order() -> None:
    assert run(["list"], LISTED).stdout == (
        "tc-aaaaaaaa dead work w\n"
        "tc-bbbbbbbb running termctl KEY-12\n"
        "tc-cccccccc running termctl KEY-12\n"
    )


@pytest.mark.parametrize(
    ("flags", "ids"),
    [
        (["--label", "kind=review"], ["tc-aaaaaaaa", "tc-bbbbbbbb", "tc-cccccccc"]),
        (["--label", "kind=review", "--label", "repo=x"], ["tc-bbbbbbbb"]),
        (["--label", "repo=x", "--label", "repo=x"], ["tc-bbbbbbbb"]),
        (["--label", "kind="], []),
        (["--session", "work"], ["tc-aaaaaaaa"]),
        (["--session", "wor"], []),
        (["--dead"], ["tc-aaaaaaaa"]),
        (["--dead", "--session", "termctl"], []),
    ],
)
def test_list_filters(flags: list[str], ids: list[str]) -> None:
    result = run(["--json", "list", *flags], LISTED)
    assert result.code == 0
    validate(result.json(), "termctl/cli/list.json")
    assert [pane["id"] for pane in result.json()["panes"]] == ids


def test_list_takes_more_than_32_label_filters() -> None:
    flags = [arg for index in range(33) for arg in ("--label", f"k{index:02d}=v")]
    result = run(["--json", "list", *flags], LISTED)
    assert (result.code, result.json()["panes"]) == (0, [])


def test_list_session_filter_takes_any_text_and_matches_exactly() -> None:
    rows = [pane_row(overrides={"session_name": "a b\\033"})]
    assert [
        p["id"] for p in run(["--json", "list", "--session", "a b\\033"], rows).json()["panes"]
    ] == [ID]


@pytest.mark.parametrize("gateway", [no_socket, no_parent], ids=["no-socket", "no-parent"])
def test_list_without_a_server_is_empty_and_never_connects(gateway: Any) -> None:
    result = run(["--json", "list"], gateway=gateway())
    assert (result.code, result.json()) == (0, {"socket_path": SOCK, "panes": []})
    assert result.gateway.calls == [[TMUX, "-V"]]
    human = run(["list"], gateway=gateway())
    assert (human.code, human.stdout, human.stderr) == (0, "", "")


@pytest.mark.parametrize(
    "stderr", [f"no server running on {SOCK}\n", "no current target\n"], ids=["refused", "empty"]
)
def test_list_of_no_server_or_no_sessions_is_empty(stderr: str) -> None:
    gateway = FakeGateway(list_panes=completed(stderr=stderr, returncode=1))
    result = run(["--json", "list"], gateway=gateway)
    assert (result.code, result.json()) == (0, {"socket_path": SOCK, "panes": []})


def test_list_of_a_running_server_without_managed_panes() -> None:
    result = run(["--json", "list"], [])
    assert (result.code, result.json()) == (0, {"socket_path": SOCK, "panes": []})


# --- reader warnings, tmux failures and escaping ------------------------------------------------


FORGED = pane_row("%5", OTHER, {"@termctl_name": "evil\x1b[2J\x9b"})

FORGED_OPTIONS = {
    "name": (
        {"@termctl_name": "evil\x1b[2J\x9b"},
        "invalid @termctl_name 'evil\\x1b[2J\\x9b': must not contain control characters",
    ),
    "labels": ({"@termctl_labels": "k=v\x1b[2J"}, "invalid @termctl_labels 'k=v\\x1b[2J'"),
    "marker_text": (
        {"@termctl_marker": "attention", "@termctl_marker_text": "\x9b!"},
        "invalid @termctl_marker_text '\\x9b!': must not contain control characters",
    ),
}


@pytest.mark.parametrize("option", sorted(FORGED_OPTIONS))
@pytest.mark.parametrize("argv", [["list"], ["show", ID], ["exists", ID], ["--json", "list"]])
def test_reader_warnings_are_printed_escaped(argv: list[str], option: str) -> None:
    overrides, problem = FORGED_OPTIONS[option]
    result = run(argv, [RUNNING, pane_row("%5", OTHER, overrides)])
    assert result.code == 0
    assert result.stderr == f"termctl: warning: ignoring managed pane %5: {problem}\n"
    raw = [c for c in result.stderr if c != "\n" and (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F)]
    assert raw == []


def test_reader_warnings_are_printed_once_and_quiet_suppresses_them() -> None:
    assert run(["show", ID], [RUNNING, FORGED]).stderr.count("warning") == 1
    assert run(["--quiet", "list"], [RUNNING, FORGED]).stderr == ""


@pytest.mark.parametrize("argv", [["list"], ["show", ID], ["exists", ID]])
def test_other_tmux_failures_are_server_unavailable_with_escaped_text(argv: list[str]) -> None:
    failure = completed(stderr="lost server\x1b[2J\x9b\x7f\n", returncode=1)
    result = run(argv, gateway=FakeGateway(list_panes=failure))
    assert (result.code, result.stdout) == (12, "")
    assert result.stderr == "termctl: cannot list the panes: lost server\\x1b[2J\\x9b\\x7f\n"
    as_json = run(["--json", *argv], gateway=FakeGateway(list_panes=failure))
    assert as_json.json()["error"]["code"] == "server_unavailable"
    assert as_json.json()["error"]["message"] == "cannot list the panes: lost server\x1b[2J\x9b\x7f"
    assert as_json.json()["error"]["details"] == {"socket_path": SOCK}


HOSTILE = {"session_name": "s\x1b]0;x\x07", "window_name": "w\x1b[2J\x85\x9b"}


def test_tmux_text_is_escaped_in_human_output() -> None:
    row = pane_row(overrides=HOSTILE)
    expected = "tc-abcdefgh running s\\x1b]0;x\\x07 w\\x1b[2J\\x85\\x9b\n"
    assert run(["show", ID], [row]).stdout == expected
    assert run(["list"], [row]).stdout == expected


def test_json_output_is_not_escaped_as_human_output() -> None:
    row = pane_row(overrides=HOSTILE)
    shown = run(["--json", "show", ID], [row])
    assert "\\x1b" not in shown.stdout and "\x1b" not in shown.stdout
    assert (shown.json()["session"], shown.json()["display_name"]) == (
        HOSTILE["session_name"],
        HOSTILE["window_name"],
    )
    listed = run(["--json", "list"], [row]).json()["panes"][0]
    assert listed["display_name"] == HOSTILE["window_name"]


def test_verbose_traces_the_list_panes_command() -> None:
    lines = run(["--verbose", "list"]).stderr.splitlines()
    assert lines[0] == "termctl: tmux: /usr/bin/tmux -V"
    assert lines[1].startswith(f"termctl: tmux: /usr/bin/tmux -u -S {SOCK} list-panes -a -F ")
    assert len(lines) == 2


def test_tmux_runs_with_the_filtered_environment() -> None:
    environ = {"LANG": "C.UTF-8", "LC_CTYPE": "C.UTF-8", "SECRET": "x", "HOME": "/h"}
    result = run(["list"], environ=environ)
    calls = result.gateway.calls
    index = next(i for i, call in enumerate(calls) if is_list_panes(call))
    assert result.gateway.envs[index] == {
        **NO_CONFIG,
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "HOME": "/h",
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],  # the test's private one (conftest)
    }


# --- the socket checks run again right before connecting ----------------------------------------


class ChangingSocket(FakeGateway):
    """The socket's ``lstat`` gives ``first`` during the step-3 checks and ``later`` after."""

    def __init__(self, first: FileInfo | None, later: FileInfo | None) -> None:
        files = safe_default_server()
        del files[SOCK]
        super().__init__(files=files, panes=[RUNNING])
        self.states = [first, later]
        self.socket_lstats = 0

    def lstat(self, path: str) -> FileInfo:
        if path == SOCK:
            state = self.states[min(self.socket_lstats, 1)]
            self.socket_lstats += 1
            self.lstats.append(path)
            if state is None:
                raise FileNotFoundError(path)
            return state
        return super().lstat(path)


def _socket(uid: int = UID) -> FileInfo:
    return safe_default_server(uid)[SOCK] if uid == UID else FileInfo(0o140600, uid)


def test_a_socket_that_appears_after_step_3_is_checked_before_connecting() -> None:
    gateway = ChangingSocket(None, _socket())
    result = run(["--json", "list"], gateway=gateway)
    assert gateway.socket_lstats == 2
    assert [p["id"] for p in result.json()["panes"]] == [ID]


def test_a_socket_replaced_after_step_3_by_another_users_is_refused() -> None:
    gateway = ChangingSocket(_socket(), _socket(UID + 1))
    result = run(["--json", "list"], gateway=gateway)
    assert (result.code, result.json()["error"]["code"]) == (12, "server_unavailable")
    assert list_panes_calls(gateway) == []


def test_a_socket_removed_after_step_3_is_no_server_without_connecting() -> None:
    gateway = ChangingSocket(_socket(), None)
    result = run(["--json", "exists", ID], gateway=gateway)
    assert (result.code, result.json()["exists"]) == (10, False)
    assert list_panes_calls(gateway) == []


def test_an_unsafe_socket_never_reaches_list_panes() -> None:
    files = safe_default_server()
    files["/tmp"] = directory(0o777)
    for argv in (["list"], ["exists", ID], ["show", ID]):
        result = run(argv, gateway=FakeGateway(files=files, panes=[RUNNING]))
        assert result.code == 12
        assert result.gateway.calls == [[TMUX, "-V"]]
