"""The Backend protocol and its tmux implementation (design.md "Internal architecture")."""

from __future__ import annotations

import subprocess

import pytest

from termctl import panes, tmux_backend
from termctl import tmux_backend as tb
from termctl.backend import Backend, Created, SessionState, TmuxBackend
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


# --- open: server and session -------------------------------------------------------------------

SPEC = tmux_backend.WindowSpec(name="KEY-12", cwd="/", command=("/usr/bin/env", "--", "true"))


def backend_for(responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> TmuxBackend:
    return TmuxBackend(Tmux(FakeGateway(responses), TMUX, SOCK, env={}))


def argv(invocation: tmux_backend.Invocation) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, SOCK))


HAS = argv(tmux_backend.has_session("work"))
NEW_SESSION = argv(tmux_backend.new_session("work", SPEC, start_server=False, env={}))
START = argv(tmux_backend.new_session("work", SPEC, start_server=True, env={}))
NEW_WINDOW = argv(tmux_backend.new_window("work", SPEC))


@pytest.mark.parametrize(
    ("response", "state"),
    [
        (completed(), SessionState.EXISTS),
        (completed(stderr=f"no server running on {SOCK}\n", returncode=1), SessionState.NO_SERVER),
        (
            completed(
                stderr=f"error connecting to {SOCK} (No such file or directory)\n", returncode=1
            ),
            SessionState.NO_SERVER,
        ),
        (completed(stderr="can't find session: work\n", returncode=1), SessionState.MISSING),
    ],
)
def test_session_state(response: subprocess.CompletedProcess[str], state: SessionState) -> None:
    assert backend_for({HAS: response}).session_state("work") is state


@pytest.mark.parametrize(
    "response",
    [
        completed(stderr=f"error connecting to {SOCK} (Permission denied)\n", returncode=1),
        completed(stderr="can't find session: other\n", returncode=1),
        completed(stderr="hook output\ncan't find session: work\n", returncode=1),
        completed(stderr="", returncode=1),
        completed(returncode=-9),
    ],
)
def test_session_state_other_failures_are_server_unavailable(
    response: subprocess.CompletedProcess[str],
) -> None:
    with pytest.raises(TermctlError) as caught:
        backend_for({HAS: response}).session_state("work")
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    assert caught.value.details == {"socket_path": SOCK}


@pytest.mark.parametrize("start_server", [False, True])
def test_create_session_returns_the_new_ids(start_server: bool) -> None:
    key = START if start_server else NEW_SESSION
    backend = backend_for({key: completed("%7 @4\n")})
    assert backend.create_session("work", SPEC, start_server=start_server) == Created("%7", "@4")


def test_create_session_restores_env_from_the_environment_tmux_runs_with() -> None:
    # Review round 1, R1-2: the set-environment values come from the filtered environment the
    # Tmux runs with, never from the -e values.
    spec = tmux_backend.WindowSpec(name="shell", cwd="/", command=None, env=(("LANG", "x"),))
    filtered = {"LANG": "C.UTF-8"}
    invocation = tmux_backend.new_session("work", spec, start_server=False, env=filtered)
    gateway = FakeGateway({argv(invocation): completed("%7 @4\n")})
    backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env=filtered))
    assert backend.create_session("work", spec, start_server=False) == Created("%7", "@4")
    assert gateway.calls[-1][-6:] == ["set-environment", "-t", "=work:", "--", "LANG", "C.UTF-8"]


def test_create_session_duplicate_is_none() -> None:
    backend = backend_for(
        {NEW_SESSION: completed(stderr="duplicate session: work\n", returncode=1)}
    )
    assert backend.create_session("work", SPEC, start_server=False) is None


@pytest.mark.parametrize(
    "stderr",
    [
        "can't find session: work\n",
        # The last session ended and took the server with it: retried like a missing session.
        f"no server running on {SOCK}\n",
        f"error connecting to {SOCK} (No such file or directory)\n",
    ],
)
def test_create_window_missing_session_or_server_is_none(stderr: str) -> None:
    backend = backend_for({NEW_WINDOW: completed(stderr=stderr, returncode=1)})
    assert backend.create_window("work", SPEC) is None


def test_create_window_returns_the_new_ids() -> None:
    assert backend_for({NEW_WINDOW: completed("%1 @1\n")}).create_window("work", SPEC) == Created(
        "%1", "@1"
    )


@pytest.mark.parametrize(
    "response",
    [
        completed(stderr=f"no server running on {SOCK}\n", returncode=1),
        completed(stderr="can't find session: work\n", returncode=1),
        completed(stderr="create window failed: index 0 in use\n", returncode=1),
        # The chain stopped after new-session printed its ids: never taken as created.
        completed("%7 @4\n", stderr="can't find session: work\n", returncode=1),
        completed(""),
        completed("garbage\n"),
    ],
)
def test_create_session_failures_are_server_unavailable(
    response: subprocess.CompletedProcess[str],
) -> None:
    # open never exits 10: no server or a missing session here is a server problem.
    with pytest.raises(TermctlError) as caught:
        backend_for({NEW_SESSION: response}).create_session("work", SPEC, start_server=False)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)


@pytest.mark.parametrize(
    "response",
    [
        completed(stderr="server exited unexpectedly\n", returncode=1),
        completed(stderr="duplicate session: work\n", returncode=1),
        completed(stderr="can't find window: @1\n", returncode=1),
        completed("%1\n"),
    ],
)
def test_create_window_failures_are_server_unavailable(
    response: subprocess.CompletedProcess[str],
) -> None:
    with pytest.raises(TermctlError) as caught:
        backend_for({NEW_WINDOW: response}).create_window("work", SPEC)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)


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


@pytest.mark.parametrize("window_name", [None, "n"])
def test_write_pane_needs_options(window_name: str | None) -> None:
    with pytest.raises(ValueError):
        _backend({}).write_pane(PANE, {}, window_name)


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
    return _argv(probe.format_arg(tb.length_probe(*names)).build())


IDENTITY = f"{PANE.pane_id} {PANE.window_id} {PANE.pane_id} {ID}"


def test_byte_lengths_reads_each_field() -> None:
    argv = _lengths_argv("@termctl_name", "@termctl_marker_text")
    backend = _backend({argv: completed(f"{IDENTITY} 3 12\n")})
    assert backend.byte_lengths(PANE, ["@termctl_name", "@termctl_marker_text"]) == (3, 12)


def test_byte_lengths_of_a_window_name_without_a_tab_or_newline() -> None:
    argv = _lengths_argv("@termctl_name", "window_name")
    backend = _backend({argv: completed(f"{IDENTITY} 3 12 0\n")})
    assert backend.byte_lengths(PANE, ["@termctl_name", "window_name"]) == (3, 12)


@pytest.mark.parametrize(
    "result",
    [
        completed(f"{IDENTITY} 3\n"),
        completed(f"{IDENTITY} 3 x 0\n"),
        completed(""),
        completed(f"{IDENTITY} 3 4 5 0\n"),
        completed(f"{IDENTITY} -1 2 0\n"),
        completed(f"{IDENTITY} 3 4 0\n", returncode=1),
        completed(stderr="can't find pane: %3\n", returncode=1),
        # The window name holds a tab or newline, which the reader showed as a space.
        completed(f"{IDENTITY} 3 4 1\n"),
        completed(f"{IDENTITY} 3 4 x\n"),
        # Another pane answered (a server restarted and reused %3, or the pane moved).
        completed(f"%4 {PANE.window_id} {PANE.pane_id} {ID} 3 4 0\n"),
        completed(f"{PANE.pane_id} @99 {PANE.pane_id} {ID} 3 4 0\n"),
        completed(f"{PANE.pane_id} {PANE.window_id} %4 {ID} 3 4 0\n"),
        completed(f"{PANE.pane_id} {PANE.window_id} {PANE.pane_id} tc-zzzzzzzz 3 4 0\n"),
        completed(f"{PANE.pane_id} {PANE.window_id}  {ID} 3 4 0\n"),
        completed(f"{IDENTITY} 3 4 0\nextra\n"),
    ],
)
def test_byte_lengths_is_none_when_unreadable_or_another_pane(
    result: subprocess.CompletedProcess[str],
) -> None:
    argv = _lengths_argv("@termctl_name", "window_name")
    backend = _backend({argv: result})
    assert backend.byte_lengths(PANE, ["@termctl_name", "window_name"]) is None


# --- the bell's terminal (#20) ------------------------------------------------------------------

TTY_ARGV = tuple(tb.tty_query(PANE.pane_id).argv(TMUX, SOCK, utf8=True))


def test_tty_query_reads_identity_state_and_tty_of_the_pane() -> None:
    # Read-only and unguarded: a display that prints only tmux's ids, flags and device path.
    assert "display" in TTY_ARGV and "if-shell" not in TTY_ARGV
    assert TTY_ARGV[TTY_ARGV.index("-t") + 1] == PANE.pane_id
    assert TTY_ARGV[-1] == "#{pane_id} #{@termctl_pane} #{@termctl_id} #{pane_dead} #{pane_tty}"


@pytest.mark.parametrize("tty", ["/dev/pts/0", "/dev/pts/17", "/dev/ttys003"])
def test_pane_tty_of_a_running_managed_pane(tty: str) -> None:
    answer = completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 {tty}\n")
    assert _backend({TTY_ARGV: answer}).pane_tty(PANE) == tty


@pytest.mark.parametrize("tty", ["/dev/sda", "/dev/pts/../sda", "/tmp/x"])
def test_pane_tty_reports_a_path_that_is_not_a_pty_as_it_is(tty: str) -> None:
    # The bell checks the path (display.ring_bell), so it can name the cause in its warning.
    answer = completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 {tty}\n")
    assert _backend({TTY_ARGV: answer}).pane_tty(PANE) == tty


@pytest.mark.parametrize(
    "result",
    [
        # Dead, gone (display of a missing pane prints empty fields), another pane or id.
        completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 1 /dev/pts/3\n"),
        completed("    \n"),
        completed(""),
        completed(f"{PANE.pane_id}  {ID} 0 /dev/pts/3\n"),
        completed(f"{PANE.pane_id} %9 {ID} 0 /dev/pts/3\n"),
        completed(f"{PANE.pane_id} {PANE.pane_id} tc-zzzzzzzz 0 /dev/pts/3\n"),
        completed(f"%9 {PANE.pane_id} {ID} 0 /dev/pts/3\n"),
        # No terminal path at all.
        completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 \n"),
        completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 /dev/pts/3 extra\n"),
        completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 /dev/pts/3\nsecond line\n"),
        completed(f"{PANE.pane_id} {PANE.pane_id} {ID} 0 /dev/pts/3\n", returncode=1),
        completed(stderr=f"can't find pane: {PANE.pane_id}\n", returncode=1),
    ],
)
def test_pane_tty_is_none_unless_the_pane_is_still_ours_and_running(
    result: subprocess.CompletedProcess[str],
) -> None:
    assert _backend({TTY_ARGV: result}).pane_tty(PANE) is None
