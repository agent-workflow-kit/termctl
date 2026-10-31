"""``open``'s server and session step against real tmux (interface.md "Open order" step 5):
spike C2 and F2 regressions, the duplicate-session race and the default directory.

Each test runs on a private socket (``tmux_server`` in conftest.py). ``open_cmd.create_window``
runs with the fixture's environment (``HOME`` is the private directory), so a server it starts
reads only the config file a test writes there. The fixture's teardown stops exactly the
server on its socket.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from termctl import open_cmd, tmux_socket
from termctl.backend import Created, SessionState, TmuxBackend
from termctl.errors import TermctlError
from termctl.gateway import SystemGateway
from termctl.tmux_backend import Tmux, WindowSpec

from .conftest import TmuxServer, wait_for

pytestmark = pytest.mark.tmux

SPEC = WindowSpec(name="KEY-12", cwd="/", command=open_cmd.PLACEHOLDER)
# A user config that would end a detached server and remove a detached session at once.
HOSTILE_CONFIG = "set -s exit-unattached on\nset -g destroy-unattached on\n"


def user_config(server: TmuxServer, text: str) -> None:
    """The config file tmux finds through the fixture's private ``HOME``."""
    (server.root / ".tmux.conf").write_text(text, encoding="utf-8")


def create(server: TmuxServer, session: str = "work", socket_path: str | None = None) -> Created:
    gateway = SystemGateway()
    resolved = tmux_socket.resolve(socket_path or server.socket_path, None, {}, gateway)
    tmux = Tmux(gateway, server.tmux_bin, resolved.socket_path, env=server.env)
    return open_cmd.create_window(TmuxBackend(tmux), resolved, gateway, session, SPEC)


def windows(server: TmuxServer, session: str) -> list[str]:
    result = server.raw("list-windows", "-t", f"={session}:", "-F", "#{window_id} #{pane_id}")
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\n")[:-1]


def server_option(server: TmuxServer, name: str) -> str:
    return server.raw("show", "-sv", name).stdout.strip()


# --- starting a server --------------------------------------------------------------------------


def test_no_server_starts_one_with_the_session(tmux_server: TmuxServer) -> None:
    created = create(tmux_server)
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]
    name = tmux_server.raw("display", "-p", "-t", created.pane_id, "#{window_name}")
    assert name.stdout == "KEY-12\n"


def test_f2_c2_a_started_server_survives_a_hostile_user_config(tmux_server: TmuxServer) -> None:
    # Spike F2: exit-unattached on would end the server; C2: destroy-unattached on would remove
    # the detached session before a second invocation could run.
    user_config(tmux_server, HOSTILE_CONFIG)
    created = create(tmux_server)
    assert not wait_for(lambda: not tmux_server.alive(), timeout=0.5)
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]
    assert server_option(tmux_server, "exit-unattached") == "off"
    # The user's global setting is kept; only termctl's session is exempt.
    assert tmux_server.raw("show", "-gv", "destroy-unattached").stdout.strip() == "on"


def test_f2_control_without_the_prefix_the_server_ends(tmux_server: TmuxServer) -> None:
    user_config(tmux_server, HOSTILE_CONFIG)
    tmux_server.raw(
        "new-session", "-d", "-s", "x", ";", "set", "-t", "=x:", "destroy-unattached", "off"
    )
    assert wait_for(lambda: not tmux_server.alive())


@pytest.fixture
def attached(tmux_server: TmuxServer) -> Iterator[TmuxServer]:
    """A running server started by someone else, kept up by a control-mode client so that its
    own ``exit-unattached on`` does not end it."""
    tmux_server.start()
    client = subprocess.Popen(
        [tmux_server.tmux_bin, "-S", tmux_server.socket_path, "-C", "attach", "-t", "=base:"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=tmux_server.env,
    )
    try:
        assert wait_for(lambda: "attached" in tmux_server.raw("list-clients").stdout)
        yield tmux_server
    finally:
        assert client.stdin is not None
        client.stdin.close()
        client.wait(timeout=10)


def test_f2_a_running_server_keeps_its_settings(attached: TmuxServer) -> None:
    attached.raw("set", "-s", "exit-unattached", "on")
    created = create(attached)
    assert windows(attached, "work") == [f"{created.window_id} {created.pane_id}"]
    assert server_option(attached, "exit-unattached") == "on"


def test_c2_a_new_session_on_a_running_server_survives_destroy_unattached(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    tmux_server.raw("set", "-g", "destroy-unattached", "on")
    created = create(tmux_server)
    assert not wait_for(lambda: tmux_server.raw("has-session", "-t", "=work:").returncode != 0, 0.5)
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]


def test_an_existing_session_gets_a_new_window(tmux_server: TmuxServer) -> None:
    tmux_server.start("work")
    before = windows(tmux_server, "work")
    created = create(tmux_server)
    assert windows(tmux_server, "work") == [*before, f"{created.window_id} {created.pane_id}"]
    # -d: the new window never becomes the current one.
    current = tmux_server.raw("display", "-p", "-t", "=work:", "#{window_id}").stdout.strip()
    assert current != created.window_id


def test_the_session_is_matched_exactly(tmux_server: TmuxServer) -> None:
    tmux_server.start("workbench")
    create(tmux_server, "work")
    assert len(windows(tmux_server, "workbench")) == 1
    assert len(windows(tmux_server, "work")) == 1


# --- the duplicate-session race -----------------------------------------------------------------


def race(server: TmuxServer, count: int) -> list[Created | BaseException]:
    results: list[Created | BaseException] = []
    barrier = threading.Barrier(count)

    def one() -> None:
        barrier.wait()
        try:
            results.append(create(server))
        except BaseException as exc:  # noqa: BLE001 (reported by the test)
            results.append(exc)

    threads = [threading.Thread(target=one) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@pytest.mark.parametrize("running", [True, False], ids=["running-server", "no-server"])
def test_concurrent_opens_all_create_their_window(tmux_server: TmuxServer, running: bool) -> None:
    if running:
        tmux_server.start()
    results = race(tmux_server, 4)
    created = [r for r in results if isinstance(r, Created)]
    assert len(created) == 4, results
    assert sorted(windows(tmux_server, "work")) == sorted(
        f"{c.window_id} {c.pane_id}" for c in created
    )


# --- the socket's directory ---------------------------------------------------------------------


@pytest.fixture
def tmpdir_base(tmux_server: TmuxServer) -> Iterator[Path]:
    """A private ``TMUX_TMPDIR``; the server started in it is stopped by its exact socket."""
    base = Path(tempfile.mkdtemp(prefix="tc-", dir="/tmp"))
    try:
        yield base
    finally:
        socket = base / f"tmux-{os.getuid()}" / "work"
        subprocess.run(
            [tmux_server.tmux_bin, "-S", str(socket), "kill-server"],
            capture_output=True,
            env=tmux_server.env,
            timeout=30,
            check=False,
        )
        shutil.rmtree(base, ignore_errors=True)


def test_a_missing_default_directory_is_created_0700(
    tmux_server: TmuxServer, tmpdir_base: Path
) -> None:
    gateway = SystemGateway()
    server = tmux_socket.resolve("work", None, {"TMUX_TMPDIR": str(tmpdir_base)}, gateway)
    tmux = Tmux(gateway, tmux_server.tmux_bin, server.socket_path, env=tmux_server.env)
    old = os.umask(0o022)
    try:
        open_cmd.create_window(TmuxBackend(tmux), server, gateway, "work", SPEC)
    finally:
        os.umask(old)
    directory = tmpdir_base / f"tmux-{os.getuid()}"
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    assert stat.S_ISSOCK((directory / "work").lstat().st_mode)


def test_an_explicit_path_with_a_missing_parent_starts_nothing(tmux_server: TmuxServer) -> None:
    missing = tmux_server.root / "missing"
    with pytest.raises(TermctlError) as caught:
        create(tmux_server, socket_path=str(missing / "s"))
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    assert not missing.exists()


def test_an_unsafe_socket_directory_is_never_connected(tmux_server: TmuxServer) -> None:
    unsafe = tmux_server.root / "shared"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    with pytest.raises(TermctlError) as caught:
        create(tmux_server, socket_path=str(unsafe / "s"))
    assert caught.value.code == "server_unavailable"
    assert list(unsafe.iterdir()) == []


Other = tuple[str, ...] | Callable[[], None]


class LateProbe(TmuxBackend):
    """A backend whose probe answers what it found, after which ``other`` runs (an argv, or a
    function): another caller acting between termctl's probe and its creation."""

    def __init__(self, tmux: Tmux, other: list[Other]) -> None:
        super().__init__(tmux)
        self.other = other

    def session_state(self, session: str) -> SessionState:
        state = super().session_state(session)
        if self.other:
            other = self.other.pop(0)
            if callable(other):
                other()
            else:
                subprocess.run(other, check=True, capture_output=True, timeout=30)
        return state


def late(server: TmuxServer, *other: Other) -> tuple[Created, list[str]]:
    gateway = SystemGateway()
    resolved = tmux_socket.resolve(server.socket_path, None, {}, gateway)
    traces: list[str] = []
    tmux = Tmux(gateway, server.tmux_bin, resolved.socket_path, env=server.env, trace=traces.append)
    created = open_cmd.create_window(LateProbe(tmux, list(other)), resolved, gateway, "work", SPEC)
    return created, [t.split()[4] for t in traces]


def raw_argv(server: TmuxServer, *args: str) -> tuple[str, ...]:
    return (server.tmux_bin, "-S", server.socket_path, *args)


def test_duplicate_session_falls_back_to_new_window(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    other = raw_argv(tmux_server, "new-session", "-d", "-s", "work")
    created, commands = late(tmux_server, other)
    assert commands == ["has-session", "new-session", "new-window"]
    assert windows(tmux_server, "work")[-1] == f"{created.window_id} {created.pane_id}"
    assert len(windows(tmux_server, "work")) == 2


def test_vanished_session_is_retried_by_the_path_the_probe_selects(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    tmux_server.raw("new-session", "-d", "-s", "work")
    other = raw_argv(tmux_server, "kill-session", "-t", "=work:")
    created, commands = late(tmux_server, other)
    assert commands == ["has-session", "new-window", "has-session", "new-session", "has-session"]
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]


def test_a_vanished_last_session_that_took_the_server_is_retried(tmux_server: TmuxServer) -> None:
    # The session is the server's last: killing it ends the server (exit-empty), so new-window
    # finds no server; the retry starts one.
    tmux_server.start("work")

    def kill_last_session() -> None:
        tmux_server.raw("kill-session", "-t", "=work:")
        assert wait_for(lambda: not tmux_server.alive())

    created, commands = late(tmux_server, kill_last_session)
    assert commands[:2] == ["has-session", "new-window"]
    assert commands[-2:] == ["set", "has-session"]
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]


class ServerEndsAtNewSession(TmuxBackend):
    """A backend where the server ends right before the first ``new-session`` that does not
    start a server, so that ``new-session`` starts one without the prefix; the user config
    ends that one at once."""

    def __init__(self, tmux: Tmux, server: TmuxServer) -> None:
        super().__init__(tmux)
        self.server = server
        self.armed = True

    def create_session(
        self, session: str, spec: WindowSpec, *, start_server: bool
    ) -> Created | None:
        if not self.armed or start_server:
            return super().create_session(session, spec, start_server=start_server)
        self.armed = False
        self.server.raw("kill-server")
        assert wait_for(lambda: not self.server.alive())
        created = super().create_session(session, spec, start_server=False)
        assert wait_for(lambda: not self.server.alive())
        return created


def test_a_server_started_by_new_session_without_the_prefix_is_retried(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    user_config(tmux_server, HOSTILE_CONFIG)
    gateway = SystemGateway()
    resolved = tmux_socket.resolve(tmux_server.socket_path, None, {}, gateway)
    tmux = Tmux(gateway, tmux_server.tmux_bin, resolved.socket_path, env=tmux_server.env)
    backend = ServerEndsAtNewSession(tmux, tmux_server)
    created = open_cmd.create_window(backend, resolved, gateway, "work", SPEC)
    assert not backend.armed
    assert not wait_for(lambda: not tmux_server.alive(), timeout=0.5)
    assert windows(tmux_server, "work") == [f"{created.window_id} {created.pane_id}"]
    assert server_option(tmux_server, "exit-unattached") == "off"


def test_a_socket_directory_made_unsafe_after_the_probe_is_never_connected(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start("work")
    try:
        with pytest.raises(TermctlError) as caught:
            late(tmux_server, lambda: tmux_server.root.chmod(0o777))
    finally:
        tmux_server.root.chmod(0o700)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    assert "writable by group or others" in caught.value.message
    assert len(windows(tmux_server, "work")) == 1


def test_the_socket_peer_is_the_server_that_reports_its_pid(tmux_server: TmuxServer) -> None:
    # Open order step 7 (review round 1 of #16, R1-8): the server termctl reaches through the
    # socket (SO_PEERCRED) is the one whose #{pid} the query reports, in the same namespace.
    tmux_server.start()
    reported = tmux_server.raw("display", "-p", "#{pid}")
    assert reported.returncode == 0, reported.stderr
    assert SystemGateway().socket_peer_pid(tmux_server.socket_path) == int(reported.stdout)
    assert tmux_server.alive()
