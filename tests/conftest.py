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
import pwd
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from termctl import statedir
from termctl.gateway import SystemGateway
from termctl.tmux_backend import Tmux

TMUX_BIN = shutil.which("tmux")


def tmux_version(tmux_bin: str | None = TMUX_BIN) -> tuple[int, int] | None:
    """``(major, minor)`` of ``tmux_bin`` (``tmux 3.2a`` is ``(3, 2)``); ``None`` without one."""
    if tmux_bin is None:
        return None
    out = subprocess.run([tmux_bin, "-V"], capture_output=True, text=True, timeout=10).stdout
    match = re.search(r"([0-9]+)\.([0-9]+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


TMUX_VERSION = tmux_version()


# --- the guard against tmux reading the real user's config (review round 1, R1-11) -------------

# The real home directories, recorded before the private-environment fixture replaces HOME:
# a tmux server started with HOME absent takes the home directory from the passwd entry.
REAL_HOMES = frozenset(
    os.path.realpath(home)
    for home in (os.environ.get("HOME"), pwd.getpwuid(os.getuid()).pw_dir)
    if home
)
# tmux client options that take a value (tmux 3.2a to 3.6a).
_TMUX_VALUE_OPTIONS = frozenset({"-S", "-L", "-f", "-c", "-T"})


def _reads_no_config(argv: list[str]) -> bool:
    """Whether the tmux client options of ``argv`` include ``-f`` (a server it starts reads
    that file, never one under HOME) or ``-V`` (tmux prints its version and exits)."""
    index = 1
    while index < len(argv) and argv[index].startswith("-") and argv[index] != "--":
        if argv[index] in ("-f", "-V"):
            return True
        index += 2 if argv[index] in _TMUX_VALUE_OPTIONS else 1
    return False


def _under_real_home(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == home or real.startswith(home.rstrip("/") + "/") for home in REAL_HOMES)


def tmux_home_problem(
    argv: object, env: Mapping[str, str] | None, executable: object = None
) -> str | None:
    """Why running ``argv`` with ``env`` (``None``: this process's) could make tmux read the
    real user's config, or None when it cannot.

    Checked: a list or tuple ``argv`` whose ``argv[0]``, or the ``executable`` Popen argument
    when given, has the basename ``tmux``, and whose tmux client options include neither
    ``-f`` nor ``-V``. Such a run is refused when ``HOME`` is absent or a real home directory,
    or when ``XDG_CONFIG_HOME`` is set and its realpath is a real home or under one (tmux >=
    3.2 reads ``$XDG_CONFIG_HOME/tmux/tmux.conf``; an absent one falls back to the private
    ``HOME``). Not checked: a string ``argv`` (``shell=True`` command strings included) and
    tmux run by another program (``/usr/bin/env tmux``).
    """
    if isinstance(argv, (str, bytes, os.PathLike)) or not isinstance(argv, (list, tuple)):
        return None
    elements = [os.fsdecode(element) for element in argv]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if not elements or _reads_no_config(elements):
        return None
    names = {os.path.basename(elements[0])}
    if isinstance(executable, (str, bytes, os.PathLike)):
        names.add(os.path.basename(os.fsdecode(executable)))  # pyright: ignore[reportUnknownArgumentType]
    if "tmux" not in names:
        return None
    environ = os.environ if env is None else env
    home = environ.get("HOME")
    if not home:
        return "HOME is not set, so tmux would read the passwd home's config"
    if os.path.realpath(home) in REAL_HOMES:
        return f"HOME is the real home {home}, so tmux would read its config"
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg and _under_real_home(xdg):
        return f"XDG_CONFIG_HOME {xdg} is under a real home, so tmux would read its config"
    return None


class _GuardedPopen(subprocess.Popen):  # type: ignore[type-arg]  # pyright: ignore[reportMissingTypeArgument]
    """``subprocess.Popen`` that refuses a tmux run that could read the real config."""

    def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
        # Popen's positional order: bufsize, executable, ..., cwd, env.
        env = kwargs.get("env")
        if env is None and len(rest) >= 10:
            env = rest[9]
        executable = kwargs.get("executable")
        if executable is None and len(rest) >= 2:
            executable = rest[1]
        problem = tmux_home_problem(args, env, executable)
        if problem is not None:
            raise AssertionError(f"test guard: refusing to run {args[0]!r}: {problem}")
        super().__init__(args, *rest, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def private_home() -> Iterator[Path]:
    """The whole run gets private HOME, XDG_CONFIG_HOME and TMUX_TMPDIR directories, and every
    tmux run is checked by ``tmux_home_problem``: a test that would let tmux read the real
    ~/.tmux.conf fails instead."""
    root = Path(tempfile.mkdtemp(prefix="tc-home-"))
    for name in ("home", "config", "tmux"):
        (root / name).mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HOME", str(root / "home"))
        patch.setenv("XDG_CONFIG_HOME", str(root / "config"))
        patch.setenv("TMUX_TMPDIR", str(root / "tmux"))
        patch.setattr(subprocess, "Popen", _GuardedPopen)
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)


# --- the guard against writing the real user's state directory (review round 1, R1-1) ---------


def real_state_dir_problem(path: Path) -> str | None:
    """Why a state directory at ``path`` must not be used by a test, or None when it may."""
    if _under_real_home(str(path)):
        return f"state directory {path} is under a real home directory"
    return None


def private_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` for ``main`` with this test's private ``XDG_STATE_HOME`` added when it sets
    none (see ``private_state_home``)."""
    return {"XDG_STATE_HOME": os.environ["XDG_STATE_HOME"], **environ}


@pytest.fixture(autouse=True)
def private_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Each test gets a private ``XDG_STATE_HOME`` in this process's environment, which the
    environ helpers (``tests.test_cli.ENVIRON`` users) pass on to ``main``, and every state
    directory termctl resolves is checked by ``real_state_dir_problem``: a test whose environ
    would let termctl write the real ``~/.local/state/termctl`` fails instead."""
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    resolve = statedir.state_dir

    def guarded(environ: Mapping[str, str]) -> Path:
        path = resolve(environ)
        problem = real_state_dir_problem(path)
        if problem is not None:
            raise AssertionError(f"test guard: {problem}")
        return path

    monkeypatch.setattr(statedir, "state_dir", guarded)
    yield state_home


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
