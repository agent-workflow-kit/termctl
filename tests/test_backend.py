"""The Backend protocol and its tmux implementation (design.md "Internal architecture")."""

from __future__ import annotations

import subprocess

from termctl import panes
from termctl.backend import Backend, TmuxBackend
from termctl.tmux_backend import Tmux

from .fakes import FakeGateway

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
