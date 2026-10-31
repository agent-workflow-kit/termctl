"""The Backend protocol and its tmux implementation (design.md "Internal architecture")."""

from __future__ import annotations

import subprocess

import pytest

from termctl import panes
from termctl import tmux_backend as tb
from termctl.backend import Backend, TmuxBackend
from termctl.errors import TermctlError
from termctl.tmux_backend import Invocation, Tmux, command

from .fakes import FakeGateway, completed, list_panes_stdout, pane_row

TMUX = "/usr/bin/tmux"
SOCK = "/tmp/tmux-1000/default"
SENTINEL = "termctl-end-0123456789abcdef"


def test_tmux_backend_reads_panes_through_the_reader() -> None:
    argv = panes.invocation(SENTINEL).argv(TMUX, SOCK, utf8=True)
    gateway = FakeGateway({tuple(argv): subprocess.CompletedProcess(argv, 0, "", "")})
    backend: Backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env={}), sentinel=lambda: SENTINEL)
    assert backend.socket_path == SOCK
    assert backend.read_panes() == panes.PaneRead(server_running=True, panes=(), warnings=())
    assert gateway.calls == [argv]


# --- guarded writes (#19) -----------------------------------------------------------------------

PANE = panes.parse(list_panes_stdout([pane_row()], SENTINEL), SENTINEL)[0][0]
ID = PANE.id


def _backend(responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> TmuxBackend:
    return TmuxBackend(Tmux(FakeGateway(responses), TMUX, SOCK, env={}), sentinel=lambda: SENTINEL)


def _argv(*commands: tb.Command) -> tuple[str, ...]:
    return tuple(Invocation.of(*commands).argv(TMUX, SOCK, utf8=True))


def _set(name: str, value: str) -> tb.Command:
    return command("set").flag("-p").target(tb.pane(PANE.pane_id)).arg(name).arg(value).build()


def _rename(name: str) -> tb.Command:
    return command("rename-window").target(tb.pane(PANE.pane_id)).name(name).build()


def _with_window(*inner: tb.Command) -> tuple[str, ...]:
    return _argv(tb.guarded(tb.pane(PANE.pane_id), tb.owned_single_pane_window(ID), *inner))


def _options_only(*inner: tb.Command) -> tuple[str, ...]:
    return _argv(tb.guarded(tb.pane(PANE.pane_id), tb.owned(ID), *inner))


NOMATCH = completed(tb.NOMATCH + "\n")


def test_write_pane_renames_the_window_in_the_same_guard() -> None:
    argv = _with_window(_set("@termctl_name", "new"), _rename("new"))
    backend = _backend({argv: completed()})
    assert backend.write_pane(PANE, {"@termctl_name": "new"}, "new") is True
    assert backend.tmux.gateway.calls == [list(argv)]  # type: ignore[attr-defined]


def test_write_pane_falls_back_to_the_options_when_the_window_is_not_its_own() -> None:
    combined = _with_window(_set("@termctl_name", "new"), _rename("new"))
    alone = _options_only(_set("@termctl_name", "new"))
    backend = _backend({combined: NOMATCH, alone: completed()})
    assert backend.write_pane(PANE, {"@termctl_name": "new"}, "new") is False
    assert backend.tmux.gateway.calls == [list(combined), list(alone)]  # type: ignore[attr-defined]


def test_write_pane_without_a_window_name_sets_the_options_only() -> None:
    alone = _options_only(_set("@termctl_labels", "a=b"), _set("@termctl_name", "n"))
    backend = _backend({alone: completed()})
    assert backend.write_pane(PANE, {"@termctl_labels": "a=b", "@termctl_name": "n"}, None) is False


def test_write_pane_of_a_pane_no_longer_the_one_resolved_is_not_found() -> None:
    combined = _with_window(_set("@termctl_name", "new"), _rename("new"))
    alone = _options_only(_set("@termctl_name", "new"))
    backend = _backend({combined: NOMATCH, alone: NOMATCH})
    with pytest.raises(TermctlError) as caught:
        backend.write_pane(PANE, {"@termctl_name": "new"}, "new")
    assert (caught.value.code, caught.value.exit_code) == ("not_found", 10)


def test_write_pane_without_options_checks_the_pane_with_a_guarded_display() -> None:
    combined = _with_window(_rename("n"))
    probe = command("display").flag("-p").target(tb.pane(PANE.pane_id))
    alone = _options_only(probe.format_arg(tb.fields("pane_id")).build())
    backend = _backend({combined: NOMATCH, alone: completed(PANE.pane_id + "\n")})
    assert backend.write_pane(PANE, {}, "n") is False


def test_write_pane_needs_something_to_write() -> None:
    with pytest.raises(ValueError):
        _backend({}).write_pane(PANE, {}, None)


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (f"can't find pane: {PANE.pane_id}", "not_found"),
        (f"no server running on {SOCK}", "not_found"),
        ("something else", "server_unavailable"),
    ],
)
def test_write_pane_tmux_failures(stderr: str, code: str) -> None:
    combined = _with_window(_set("@termctl_name", "new"), _rename("new"))
    backend = _backend({combined: completed(stderr=stderr + "\n", returncode=1)})
    with pytest.raises(TermctlError) as caught:
        backend.write_pane(PANE, {"@termctl_name": "new"}, "new")
    assert caught.value.code == code


def test_write_pane_failure_of_the_options_only_guard() -> None:
    combined = _with_window(_set("@termctl_name", "new"), _rename("new"))
    alone = _options_only(_set("@termctl_name", "new"))
    backend = _backend({combined: NOMATCH, alone: completed(stderr="boom\n", returncode=1)})
    with pytest.raises(TermctlError) as caught:
        backend.write_pane(PANE, {"@termctl_name": "new"}, "new")
    assert caught.value.code == "server_unavailable"


def _lengths_argv(*names: str) -> tuple[str, ...]:
    probe = command("display").flag("-p").target(tb.pane(PANE.pane_id))
    return _argv(probe.format_arg(tb.byte_lengths(*names)).build())


def test_byte_lengths_reads_each_field() -> None:
    argv = _lengths_argv("@termctl_name", "window_name")
    backend = _backend({argv: completed("3 12\n")})
    assert backend.byte_lengths(PANE.pane_id, ["@termctl_name", "window_name"]) == (3, 12)


@pytest.mark.parametrize(
    "result",
    [
        completed("3\n"),
        completed("3 x\n"),
        completed(""),
        completed("3 4 5\n"),
        completed("-1 2\n"),
        completed("3 4\n", returncode=1),
        completed(stderr="can't find pane: %3\n", returncode=1),
    ],
)
def test_byte_lengths_is_none_when_unreadable(result: subprocess.CompletedProcess[str]) -> None:
    argv = _lengths_argv("@termctl_name", "window_name")
    backend = _backend({argv: result})
    assert backend.byte_lengths(PANE.pane_id, ["@termctl_name", "window_name"]) is None
