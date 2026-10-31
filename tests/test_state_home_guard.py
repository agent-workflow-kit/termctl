"""The test-suite guard against writing the real user's state directory (conftest.py
``private_state_home``; review round 1 of #16, R1-1). Nothing is written here: the guard
refuses when the state directory is resolved, before anything is created or opened."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from termctl import jsonlog, statedir

from .conftest import REAL_HOMES, private_environ

REAL_HOME = next(iter(REAL_HOMES))


def test_each_test_has_a_private_state_home(private_state_home: Path) -> None:
    assert os.environ["XDG_STATE_HOME"] == str(private_state_home)
    assert not any(
        Path(os.path.realpath(private_state_home)).is_relative_to(home) for home in REAL_HOMES
    )


def test_private_environ_adds_the_private_state_home_only_when_unset(
    private_state_home: Path,
) -> None:
    assert private_environ({"PATH": "/usr/bin"}) == {
        "XDG_STATE_HOME": str(private_state_home),
        "PATH": "/usr/bin",
    }
    assert private_environ({"XDG_STATE_HOME": "/s"}) == {"XDG_STATE_HOME": "/s"}


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"HOME": REAL_HOME},
        {"XDG_STATE_HOME": os.path.join(REAL_HOME, ".local", "state")},
    ],
    ids=["passwd-home", "real-home", "state-home-under-real-home"],
)
def test_the_guard_refuses_a_state_dir_under_the_real_home(env: dict[str, str]) -> None:
    with pytest.raises(AssertionError, match="test guard"):
        statedir.open_state_dir(env)
    with pytest.raises(AssertionError, match="test guard"):
        jsonlog.append_log(env, {"event": "warning"})


def test_the_guard_allows_a_private_state_dir(tmp_path: Path) -> None:
    fd, path = statedir.open_state_dir({"XDG_STATE_HOME": str(tmp_path)})
    os.close(fd)
    assert path == str(tmp_path / "termctl")
