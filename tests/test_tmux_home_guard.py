"""The test-suite guard against tmux reading the real user's config (conftest.py
``private_home``; review round 1, R1-11). No tmux runs here: the guard refuses before the
process starts, and an allowed run of a missing binary fails with ``FileNotFoundError``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from termctl.gateway import SystemGateway

from .conftest import REAL_HOMES, tmux_home_problem

MISSING_TMUX = "/nonexistent-termctl-test/tmux"


def test_the_run_has_a_private_home_config_and_tmux_tmpdir(private_home: Path) -> None:
    for name in ("HOME", "XDG_CONFIG_HOME", "TMUX_TMPDIR"):
        assert Path(os.environ[name]).is_relative_to(private_home)
    assert os.path.realpath(os.environ["HOME"]) not in REAL_HOMES


@pytest.mark.parametrize(
    "env",
    [{"PATH": "/usr/bin"}, {"PATH": "/usr/bin", "HOME": ""}, {"HOME": next(iter(REAL_HOMES))}],
    ids=["no-home", "empty-home", "real-home"],
)
def test_the_guard_refuses_tmux_without_a_private_home(env: dict[str, str]) -> None:
    with pytest.raises(AssertionError, match="test guard"):
        subprocess.Popen([MISSING_TMUX, "-S", "/tmp/x", "new-session"], env=env)
    with pytest.raises(AssertionError, match="test guard"):
        SystemGateway().run([MISSING_TMUX, "-S", "/tmp/x", "new-session"], timeout=5, env=env)


def test_the_guard_allows_a_private_home_and_an_explicit_config(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        subprocess.Popen([MISSING_TMUX, "new-session"], env={"HOME": str(tmp_path)})
    with pytest.raises(FileNotFoundError):
        subprocess.Popen([MISSING_TMUX, "-S", "/tmp/x", "-f", "/dev/null", "new-session"], env={})
    with pytest.raises(FileNotFoundError):
        # The inherited environment is the run's private one.
        subprocess.Popen([MISSING_TMUX, "new-session"])
    with pytest.raises(FileNotFoundError):
        # tmux -V prints its version and exits, whatever the environment.
        subprocess.Popen([MISSING_TMUX, "-V"], env={})


def test_the_guard_reads_only_tmux_client_options() -> None:
    assert tmux_home_problem([MISSING_TMUX, "list-panes", "-f", "x"], {}) is not None
    assert tmux_home_problem([MISSING_TMUX, "-S", "-f", "ls"], {}) is not None
    assert tmux_home_problem(["/usr/bin/env", "tmux"], {}) is None
