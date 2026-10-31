from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
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
def umask() -> Iterator[Callable[[int], int]]:
    """Set the process umask for the test; restored afterwards."""
    saved = os.umask(0o022)
    os.umask(saved)
    yield os.umask
    os.umask(saved)


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
    fd, path = statedir.open_state_dir(environ)
    opened.append(fd)
    assert path == str(state / "termctl")
    mode = (state / "termctl").stat().st_mode
    assert stat.S_ISDIR(mode)
    assert stat.S_IMODE(mode) == 0o700


def test_missing_ancestors_are_created_0700_whatever_the_umask(
    tmp_path: Path, opened: list[int], umask: Callable[[int], int]
) -> None:
    umask(0o002)
    env = {"XDG_STATE_HOME": str(tmp_path / "a" / "b")}
    opened.append(statedir.open_state_dir(env)[0])
    for path in (tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "termctl"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_created_dirs_and_files_have_exact_modes_under_a_narrow_umask(
    environ: dict[str, str], state: Path, opened: list[int], umask: Callable[[int], int]
) -> None:
    umask(0o277)
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    opened.append(statedir.open_subdir(parent, path, "locks"))
    opened.append(statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND))
    assert stat.S_IMODE((state / "termctl").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "termctl" / "locks").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "termctl" / "log.jsonl").stat().st_mode) == 0o600


def test_a_dir_swapped_for_a_symlink_after_mkdir_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask: Callable[[int], int]
) -> None:
    """Someone with write access to the parent replaces the new dir with a symlink between
    termctl's mkdir and its mode fix: the symlink target's mode must not change."""
    umask(0o002)
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o755)
    real_mkdir, real_rmdir, real_symlink = os.mkdir, os.rmdir, os.symlink

    def mkdir_then_swap(name: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)
        real_rmdir(name, dir_fd=dir_fd)
        real_symlink(target, name, dir_fd=dir_fd)

    monkeypatch.setattr(statedir.os, "mkdir", mkdir_then_swap)
    env = {"XDG_STATE_HOME": str(tmp_path / "a")}
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir(env)
    monkeypatch.undo()
    assert str(info.value) == f"{tmp_path / 'a'}: is a symlink"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert list(target.iterdir()) == []


def test_a_dangling_symlink_at_a_missing_component_is_refused(
    tmp_path: Path, opened: list[int]
) -> None:
    (tmp_path / "a").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir({"XDG_STATE_HOME": str(tmp_path / "a" / "b")})
    assert str(info.value) == f"{tmp_path / 'a'}: is a symlink"
    assert not (tmp_path / "elsewhere").exists()


def test_existing_ancestors_keep_their_mode(
    tmp_path: Path, opened: list[int], umask: Callable[[int], int]
) -> None:
    umask(0o002)
    (tmp_path / "a").mkdir()
    os.chmod(tmp_path / "a", 0o775)
    opened.append(statedir.open_state_dir({"XDG_STATE_HOME": str(tmp_path / "a")})[0])
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o775


def test_open_accepts_an_existing_state_dir(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    (state / "termctl").mkdir(parents=True, mode=0o755)
    opened.append(statedir.open_state_dir(environ)[0])


def test_state_dir_may_be_a_symlink_to_an_owned_dir(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    state.mkdir()
    (state / "termctl").symlink_to(tmp_path / "elsewhere")
    opened.append(statedir.open_state_dir(environ)[0])


@pytest.mark.parametrize(
    ("mode", "who"), [(0o770, "group"), (0o707, "others"), (0o777, "group and others")]
)
def test_group_or_world_writable_state_dir_is_refused(
    environ: dict[str, str], state: Path, mode: int, who: str
) -> None:
    make_state(state)
    os.chmod(state / "termctl", mode)
    path = str(state / "termctl")
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir(environ)
    assert str(info.value) == (
        f"{path}: writable by {who} (mode {mode:04o}); run: chmod go-w '{path}'"
    )
    assert info.value.path == path
    assert info.value.problem == f"writable by {who} (mode {mode:04o})"


def test_state_dir_that_is_a_file_is_refused(environ: dict[str, str], state: Path) -> None:
    state.mkdir()
    (state / "termctl").write_text("x")
    with pytest.raises(StateDirError):
        statedir.open_state_dir(environ)


def test_state_dir_owned_by_someone_else_is_refused(
    environ: dict[str, str], state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_state(state)
    other = os.geteuid() + 1
    monkeypatch.setattr(statedir.os, "geteuid", lambda: other)
    with pytest.raises(StateDirError, match="not owned by the current user"):
        statedir.open_state_dir(environ)


def test_nothing_is_created_under_a_parent_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. root under ``sudo -E`` must not create root-owned dirs in the user's home."""
    other = os.geteuid() + 1
    monkeypatch.setattr(statedir.os, "geteuid", lambda: other)
    env = {"XDG_STATE_HOME": str(tmp_path / "a" / "b")}
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir(env)
    assert info.value.problem == f"parent {tmp_path} not owned by the current user"
    assert info.value.path == str(tmp_path / "a" / "b" / "termctl")
    assert list(tmp_path.iterdir()) == []


def test_subdir_is_created_private(environ: dict[str, str], state: Path, opened: list[int]) -> None:
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    opened.append(statedir.open_subdir(parent, path, "locks"))
    assert stat.S_IMODE((state / "termctl" / "locks").stat().st_mode) == 0o700
    opened.append(statedir.open_subdir(parent, path, "locks"))  # existing is fine


def test_symlinked_subdir_is_refused(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    (tmp_path / "target").mkdir(mode=0o700)
    make_state(state)
    (state / "termctl" / "locks").symlink_to(tmp_path / "target")
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError) as info:
        statedir.open_subdir(parent, path, "locks")
    assert str(info.value) == f"{state / 'termctl' / 'locks'}: is a symlink"


def test_open_file_creates_a_private_regular_file(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    opened.append(statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND))
    mode = (state / "termctl" / "log.jsonl").stat().st_mode
    assert stat.S_ISREG(mode)
    assert stat.S_IMODE(mode) == 0o600


def test_open_file_refuses_a_symlink(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    make_state(state)
    (state / "termctl" / "log.jsonl").symlink_to(victim)
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError, match="is a symlink"):
        statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    assert victim.read_text() == "keep\n"


def test_open_file_refuses_a_fifo_without_blocking(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    os.mkfifo(state / "termctl" / "log.jsonl")
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    # Without a reader the open fails (ENXIO); either way it neither blocks nor writes.
    with pytest.raises(StateDirError):
        statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    with pytest.raises(StateDirError, match="not a regular file"):
        statedir.open_file(parent, path, "log.jsonl", os.O_RDWR)


def test_open_file_refuses_a_directory(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    (state / "termctl" / "log.jsonl").mkdir()
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError):
        statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND)


def test_open_file_refuses_a_writable_by_others_file(
    environ: dict[str, str], state: Path, opened: list[int]
) -> None:
    make_state(state)
    (state / "termctl" / "log.jsonl").write_text("")
    os.chmod(state / "termctl" / "log.jsonl", 0o666)
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    log = str(state / "termctl" / "log.jsonl")
    with pytest.raises(StateDirError) as info:
        statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    assert str(info.value) == (
        f"{log}: writable by group and others (mode 0666); run: chmod go-w '{log}'"
    )


def test_open_file_refuses_a_hard_linked_file(
    environ: dict[str, str], state: Path, tmp_path: Path, opened: list[int]
) -> None:
    make_state(state)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    victim.chmod(0o600)
    os.link(victim, state / "termctl" / "log.jsonl")
    parent, path = statedir.open_state_dir(environ)
    opened.append(parent)
    with pytest.raises(StateDirError, match="has 2 hard links"):
        statedir.open_file(parent, path, "log.jsonl", os.O_WRONLY | os.O_APPEND)
    assert victim.read_text() == "keep\n"


def test_errors_name_the_path(environ: dict[str, str], state: Path) -> None:
    state.mkdir()
    (state / "termctl").write_text("x")
    with pytest.raises(StateDirError) as info:
        statedir.open_state_dir(environ)
    assert str(state / "termctl") in str(info.value)
