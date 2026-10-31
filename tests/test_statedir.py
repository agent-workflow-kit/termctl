from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from termctl import config, statedir
from termctl.statedir import StateDirError


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def environ(state: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(state)}


def test_state_dir_respects_xdg_state_home(tmp_path: Path) -> None:
    env = {"XDG_STATE_HOME": str(tmp_path / "s"), "HOME": str(tmp_path / "h")}
    assert statedir.state_dir(env) == tmp_path / "s" / "termctl"


def test_state_dir_defaults_to_home_local_state(tmp_path: Path) -> None:
    assert statedir.state_dir({"HOME": str(tmp_path)}) == tmp_path / ".local/state/termctl"


@pytest.mark.parametrize("value", ["", "relative/state"])
def test_empty_or_relative_xdg_state_home_is_ignored(tmp_path: Path, value: str) -> None:
    env = {"XDG_STATE_HOME": value, "HOME": str(tmp_path)}
    assert statedir.state_dir(env) == tmp_path / ".local/state/termctl"


def test_state_dir_falls_back_to_the_passwd_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Entry:
        pw_dir = str(tmp_path)

    monkeypatch.setattr(config.pwd, "getpwuid", lambda uid: Entry())
    assert statedir.state_dir({"HOME": "rel"}) == tmp_path / ".local/state/termctl"


def test_no_home_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> Any:
        raise KeyError(uid)

    monkeypatch.setattr(config.pwd, "getpwuid", missing)
    with pytest.raises(StateDirError, match="XDG_STATE_HOME"):
        statedir.state_dir({})


def make_state(state: Path) -> None:
    """``state/termctl`` with mode 0700 whatever the umask."""
    state.mkdir()
    (state / "termctl").mkdir(mode=0o700)


@pytest.fixture
def opened() -> Iterator[list[int]]:
    """File descriptors a test opened; closed afterwards."""
    fds: list[int] = []
    yield fds
    for fd in fds:
        os.close(fd)


def test_open_creates_the_state_dir_private(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    opened.append(statedir.open_state_dir(environ))
    mode = (state / "termctl").stat().st_mode
    assert stat.S_ISDIR(mode)
    assert stat.S_IMODE(mode) & 0o077 == 0


def test_open_accepts_an_existing_state_dir(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    (state / "termctl").mkdir(parents=True, mode=0o755)
    opened.append(statedir.open_state_dir(environ))


def test_state_dir_may_be_a_symlink_to_an_owned_dir(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    state.mkdir()
    (state / "termctl").symlink_to(tmp_path / "elsewhere")
    opened.append(statedir.open_state_dir(environ))


def test_group_or_world_writable_state_dir_is_refused(environ: dict[str, str], state: Path) -> None:
    make_state(state)
    os.chmod(state / "termctl", 0o777)
    with pytest.raises(StateDirError, match="writable by others"):
        statedir.open_state_dir(environ)


def test_state_dir_that_is_a_file_is_refused(environ: dict[str, str], state: Path) -> None:
    state.mkdir()
    (state / "termctl").write_text("x")
    with pytest.raises(StateDirError):
        statedir.open_state_dir(environ)


def test_state_dir_owned_by_someone_else_is_refused(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    other = os.geteuid() + 1
    monkeypatch.setattr(statedir.os, "geteuid", lambda: other)
    with pytest.raises(StateDirError, match="not owned"):
        statedir.open_state_dir(environ)


def test_subdir_is_created_private(environ: dict[str, str], state: Path, opened: list[int]) -> None:
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    opened.append(statedir.open_subdir(parent, "locks"))
    assert stat.S_IMODE((state / "termctl" / "locks").stat().st_mode) & 0o077 == 0
    opened.append(statedir.open_subdir(parent, "locks"))  # existing is fine


def test_symlinked_subdir_is_refused(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    (tmp_path / "target").mkdir(mode=0o700)
    make_state(state)
    (state / "termctl" / "locks").symlink_to(tmp_path / "target")
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError):
        statedir.open_subdir(parent, "locks")


def test_open_file_creates_a_private_regular_file(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    opened.append(statedir.open_file(parent, "log.jsonl", os.O_WRONLY | os.O_APPEND))
    mode = (state / "termctl" / "log.jsonl").stat().st_mode
    assert stat.S_ISREG(mode)
    assert stat.S_IMODE(mode) == 0o600 & ~_umask()


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def test_open_file_refuses_a_symlink(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    make_state(state)
    (state / "termctl" / "log.jsonl").symlink_to(victim)
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError):
        statedir.open_file(parent, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    assert victim.read_text() == "keep\n"


def test_open_file_refuses_a_fifo_without_blocking(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    os.mkfifo(state / "termctl" / "log.jsonl")
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    # Without a reader the open fails (ENXIO); either way it neither blocks nor writes.
    with pytest.raises(StateDirError):
        statedir.open_file(parent, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    with pytest.raises(StateDirError, match="not a regular file"):
        statedir.open_file(parent, "log.jsonl", os.O_RDWR)


def test_open_file_refuses_a_directory(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    (state / "termctl" / "log.jsonl").mkdir()
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError):
        statedir.open_file(parent, "log.jsonl", os.O_WRONLY | os.O_APPEND)


def test_open_file_refuses_a_writable_by_others_file(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    (state / "termctl" / "log.jsonl").write_text("")
    os.chmod(state / "termctl" / "log.jsonl", 0o666)
    parent = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError, match="writable by others"):
        statedir.open_file(parent, "log.jsonl", os.O_WRONLY | os.O_APPEND)


def test_errors_name_the_path(environ: dict[str, str], state: Path) -> None:
    state.mkdir()
    (state / "termctl").write_text("x")
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir(environ)
    assert str(state / "termctl") in str(info.value)
