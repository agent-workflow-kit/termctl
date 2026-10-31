"""The Backend protocol and its tmux implementation (design.md "Internal architecture")."""

from __future__ import annotations

import subprocess

import pytest

from termctl import panes, tmux_backend
from termctl.backend import Backend, Created, SessionState, TmuxBackend
from termctl.errors import TermctlError
from termctl.tmux_backend import Tmux

from .fakes import FakeGateway, completed

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
NEW_SESSION = argv(tmux_backend.new_session("work", SPEC, start_server=False))
START = argv(tmux_backend.new_session("work", SPEC, start_server=True))
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
