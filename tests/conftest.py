"""Shared fixtures. ``tmux_server``: a private real tmux server (docs/plan.md "Conventions").

Each test gets its own server, started with ``tmux -f /dev/null -S <tmp path>``, so it never
loads a user's config or touches a user's server; teardown stops exactly that server with
``kill-server`` on its socket. Tests using it are marked ``tmux``. They are not ``live``: a
missing tmux skips them, but fails them when ``TERMCTL_REQUIRE_TMUX`` is set, so the CI job
that installs tmux (``tmux`` in .github/workflows/ci.yml) cannot pass by skipping them all.
The shared CI's ``verify`` job cannot install packages: they run there only when the runner
image ships tmux (ubuntu-26.04 ships 3.6a) and skip otherwise.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from termctl.gateway import SystemGateway
from termctl.tmux_backend import Tmux

TMUX_BIN = shutil.which("tmux")

# The state directory base (XDG_STATE_HOME) of the shared test environments, so that commands
# that log or lock (rename) never write to the user's own ~/.local/state; removed at the end.
STATE_HOME = tempfile.mkdtemp(prefix="termctl-test-state-")


@pytest.fixture(autouse=True, scope="session")
def _remove_state_home() -> Iterator[None]:
    yield
    shutil.rmtree(STATE_HOME, ignore_errors=True)


def tmux_version(tmux_bin: str | None = TMUX_BIN) -> tuple[int, int] | None:
    """``(major, minor)`` of ``tmux_bin`` (``tmux 3.2a`` is ``(3, 2)``); ``None`` without one."""
    if tmux_bin is None:
        return None
    out = subprocess.run([tmux_bin, "-V"], capture_output=True, text=True, timeout=10).stdout
    match = re.search(r"([0-9]+)\.([0-9]+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


TMUX_VERSION = tmux_version()


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

    def raw_bytes(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        """``raw`` with byte output: no newline translation, so a ``\\r`` reads back as is."""
        return subprocess.run(
            [self.tmux_bin, "-S", self.socket_path, *args],
            capture_output=True,
            timeout=30,
            env=self.env,
            check=False,
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


def tmux_missing_outcome(environ: Mapping[str, str]) -> str:
    """What a missing tmux does to a ``tmux`` test: ``"fail"`` when ``TERMCTL_REQUIRE_TMUX``
    is set, else ``"skip"``."""
    return "fail" if environ.get("TERMCTL_REQUIRE_TMUX") else "skip"


@pytest.fixture
def tmux_server() -> Iterator[TmuxServer]:
    if TMUX_BIN is None:
        if tmux_missing_outcome(os.environ) == "fail":
            pytest.fail("tmux is not installed, and TERMCTL_REQUIRE_TMUX requires the tmux tests")
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
