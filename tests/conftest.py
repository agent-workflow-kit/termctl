"""Shared fixtures. ``tmux_server``: a private real tmux server (docs/plan.md "Conventions").

Each test gets its own server, started with ``tmux -f /dev/null -S <tmp path>``, so it never
loads a user's config or touches a user's server; teardown stops exactly that server with
``kill-server`` on its socket. Tests using it are marked ``tmux`` and skipped when tmux is not
installed. They are not ``live``: CI runs them (tmux from apt).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from termctl.gateway import SystemGateway
from termctl.tmux_backend import Tmux

TMUX_BIN = shutil.which("tmux")


@dataclass
class TmuxServer:
    """A private tmux server. ``raw`` runs tmux directly, bypassing the argument builder: it
    is the tests' oracle and their way to set up hostile states."""

    tmux_bin: str
    root: Path
    socket_path: str
    env: dict[str, str]

    def raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        return SystemGateway().run(
            [self.tmux_bin, "-S", self.socket_path, *args], timeout=30, env=self.env
        )

    def start(self, session: str = "base") -> str:
        """Start the server with one session; returns its pane id."""
        result = self.raw(
            "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", session, "-x", "80", "-y", "24", "--", "/usr/bin/env", "--", "sleep", "600",
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def tmux(self, trace: list[str] | None = None) -> Tmux:
        return Tmux(
            SystemGateway(),
            self.tmux_bin,
            self.socket_path,
            env=self.env,
            trace=None if trace is None else trace.append,
        )

    def alive(self) -> bool:
        return self.raw("has-session").returncode == 0


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture
def tmux_server() -> Iterator[TmuxServer]:
    if TMUX_BIN is None:
        pytest.skip("tmux is not installed")
    # A short path: Unix socket paths are limited to about 100 bytes.
    root = Path(tempfile.mkdtemp(prefix="tc-"))
    # HOME and XDG_CONFIG_HOME point at the private directory, so even a server started by
    # mistake without -f /dev/null finds no user config.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "LANG": "C.UTF-8",
        "SHELL": "/bin/sh",
    }
    server = TmuxServer(os.path.realpath(TMUX_BIN), root, str(root / "sock"), env)
    try:
        yield server
    finally:
        server.raw("kill-server")
        shutil.rmtree(root, ignore_errors=True)
