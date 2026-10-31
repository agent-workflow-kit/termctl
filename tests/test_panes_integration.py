"""Real-tmux regressions for the pane reader: spike F4 (inherited ownership values), spike D6
(linked windows), the sanitized free-text fields, the record sentinel, liveness and the
no-server cases (interface.md "Pane options", "Liveness", "Server selection").

Each test runs its own private server (``tmux_server`` in conftest.py); panes are tagged with
``raw`` tmux calls, as a forger with socket access would.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping

import pytest

from termctl import panes
from termctl.errors import TermctlError
from termctl.panes import PaneRead

from .conftest import TMUX_VERSION, TmuxServer, wait_for

pytestmark = pytest.mark.tmux

ID = "tc-abcdefgh"
OTHER_ID = "tc-zzzzzzzz"


def options(pane_id: str, window_id: str, tc_id: str = ID) -> dict[str, str]:
    return {
        "@termctl_v": "1",
        "@termctl_pane": pane_id,
        "@termctl_window": window_id,
        "@termctl_id": tc_id,
        "@termctl_name": "KEY-12",
        "@termctl_marker": "",
        "@termctl_marker_text": "",
        "@termctl_labels": "kind=review",
        "@termctl_placement": "default",
        "@termctl_keep": "never",
        "@termctl_created": "2026-10-30T12:00:00.000Z",
        "@termctl_pid": "",
        "@termctl_pid_start": "",
    }


def set_options(server: TmuxServer, scope: list[str], values: Mapping[str, str]) -> None:
    for name, value in values.items():
        result = server.raw("set", *scope, "--", name, value)
        assert result.returncode == 0, result.stderr


def window_of(server: TmuxServer, pane_id: str) -> str:
    return server.raw("display", "-p", "-t", pane_id, "#{window_id}").stdout.strip()


def tag(server: TmuxServer, pane_id: str, tc_id: str = ID, **overrides: str) -> None:
    values = {**options(pane_id, window_of(server, pane_id), tc_id), **overrides}
    set_options(server, ["-p", "-t", pane_id], values)


def new_window(server: TmuxServer, *args: str, target: str = "=base:") -> str:
    result = server.raw(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", target, *args,
        "--", "/usr/bin/env", "--", "sleep", "600",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def read(server: TmuxServer) -> PaneRead:
    return panes.read(server.tmux())


def test_reads_a_managed_pane(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id)
    result = read(tmux_server)
    assert result.server_running and result.warnings == ()
    (pane,) = result.panes
    assert (pane.id, pane.pane_id, pane.session, pane.state) == (ID, pane_id, "base", "running")
    assert pane.window_id == window_of(tmux_server, pane_id)
    assert pane.pane_pid is not None and pane.pane_pid > 1
    assert pane.window_panes == 1
    assert pane.labels == ("kind=review",)


def test_unmanaged_panes_are_not_listed(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    new_window(tmux_server)
    assert read(tmux_server) == PaneRead(server_running=True, panes=(), warnings=())


# --- spike F4: ownership ------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [["-w"], ["-t", "=base:"], ["-g"]])
def test_inherited_values_do_not_make_other_panes_managed(
    tmux_server: TmuxServer, scope: list[str]
) -> None:
    """Every @termctl_* value set at window, session or global scope, naming a managed pane,
    is seen by every pane without its own; only the pane it names passes."""
    owner = tmux_server.start()
    tag(tmux_server, owner)
    window_scope = ["-w", "-t", window_of(tmux_server, owner)] if scope == ["-w"] else scope
    set_options(tmux_server, window_scope, options(owner, window_of(tmux_server, owner)))
    split = tmux_server.raw("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", owner)
    assert split.returncode == 0, split.stderr
    other = new_window(tmux_server)
    seen = tmux_server.raw("list-panes", "-a", "-F", "#{pane_id} #{@termctl_pane}").stdout
    # The oracle: the inherited value really is visible in the other panes.
    assert f"{split.stdout.strip()} {owner}" in seen
    if scope != ["-w"]:
        assert f"{other} {owner}" in seen
    result = read(tmux_server)
    assert [p.pane_id for p in result.panes] == [owner]
    assert result.warnings == ()


@pytest.mark.parametrize("scope", [["-w"], ["-t", "=base:"], ["-g"]])
def test_an_inherited_value_naming_no_pane_manages_nothing(
    tmux_server: TmuxServer, scope: list[str]
) -> None:
    pane_id = tmux_server.start()
    window_scope = ["-w", "-t", window_of(tmux_server, pane_id)] if scope == ["-w"] else scope
    set_options(tmux_server, window_scope, options("%999", "@999"))
    assert read(tmux_server).panes == ()


# --- spike D6: linked windows -------------------------------------------------------------------


def test_a_linked_window_gives_one_pane_with_every_session(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id)
    created = tmux_server.raw(
        "new-session", "-d", "-s", "other", "--", "/usr/bin/env", "--", "sleep", "600"
    )
    assert created.returncode == 0, created.stderr
    linked = tmux_server.raw("link-window", "-s", window_of(tmux_server, pane_id), "-t", "=other:")
    assert linked.returncode == 0, linked.stderr
    raw_rows = tmux_server.raw("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
    assert raw_rows.count(pane_id) == 2  # the oracle: tmux lists it once per session
    result = read(tmux_server)
    (pane,) = result.panes
    assert pane.session == "base"
    assert pane.sessions == ("base", "other")
    assert len(panes.select(result.panes, session="other")) == 1


# --- free text, records and decoding ------------------------------------------------------------


def test_tab_and_newline_in_names_become_spaces(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server, "-n", "a\tb\nc\x1bd")
    tag(tmux_server, pane_id)
    renamed = tmux_server.raw("rename-session", "-t", "=base:", "my\twork")
    assert renamed.returncode == 0, renamed.stderr
    (pane,) = read(tmux_server).panes
    # tmux escapes ESC itself on output; the reader replaces only tab and newline.
    assert pane.display_name == "a b c\\033d"
    assert "\t" not in pane.session and "\n" not in pane.session


def test_a_tab_in_an_option_is_flagged(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id, **{"@termctl_name": "a\tb"})
    result = read(tmux_server)
    assert result.panes == ()
    assert len(result.warnings) == 1 and pane_id in result.warnings[0]


def test_an_option_cannot_inject_a_record(tmux_server: TmuxServer) -> None:
    """A forged name holding a newline and a whole record for another pane, ended by a
    guessed sentinel, yields no pane."""
    pane_id = tmux_server.start()
    other = new_window(tmux_server)
    fake = "\t".join(
        [other, window_of(tmux_server, other), "0", "", "", "1", "1", "base", "x"]
        + list(options(other, window_of(tmux_server, other), OTHER_ID).values())
        + ["termctl-end-0000000000000000"]
    )
    tag(tmux_server, pane_id, **{"@termctl_name": "x\n" + fake})
    result = read(tmux_server)
    assert result.panes == ()
    assert len(result.warnings) == 1 and pane_id in result.warnings[0]


def test_non_ascii_is_read_faithfully_without_a_utf8_locale(tmux_server: TmuxServer) -> None:
    """tmux writes ``_`` for non-ASCII to a client without a UTF-8 locale; ``-u`` stops it."""
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id, **{"@termctl_name": "é中\U0001f514"})
    env = {key: value for key, value in tmux_server.env.items() if key != "LANG"}
    tmux = tmux_server.tmux()
    tmux.env = {**env, "LC_ALL": "C"}
    (pane,) = panes.read(tmux).panes
    assert pane.name == "é中\U0001f514"


def test_invalid_utf8_in_an_option_reads_as_tmux_escapes_it(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id, **{"@termctl_name": os.fsdecode(b"a\xc3b")})
    result = read(tmux_server)
    # tmux prints the stray byte as the text \303, a valid name; the read is still safe.
    assert [p.name for p in result.panes] == ["a\\303b"]


def test_a_c1_control_in_an_option_is_rejected(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    tag(tmux_server, pane_id, **{"@termctl_name": "a\x85b"})
    result = read(tmux_server)
    assert result.panes == () and len(result.warnings) == 1


# --- liveness -----------------------------------------------------------------------------------


def _dead_pane(server: TmuxServer, script: str) -> str:
    server.start()
    result = server.raw(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=base:",
        "--", "/usr/bin/env", "--", "sh", "-c", f"sleep 0.3; {script}",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    pane_id = result.stdout.strip()
    assert server.raw("set", "-p", "-t", pane_id, "remain-on-exit", "on").returncode == 0
    tag(server, pane_id, **{"@termctl_keep": "always"})
    assert wait_for(lambda: read(server).panes[0].state == "dead")
    return pane_id


def test_a_dead_pane_reports_its_exit_status(tmux_server: TmuxServer) -> None:
    _dead_pane(tmux_server, "exit 3")
    (pane,) = read(tmux_server).panes
    assert wait_for(lambda: read(tmux_server).panes[0].exit_status == 3)
    assert pane.exit_signal is None


@pytest.mark.skipif(TMUX_VERSION is not None and TMUX_VERSION < (3, 3), reason="tmux 3.3+")
def test_a_dead_pane_reports_its_exit_signal(tmux_server: TmuxServer) -> None:
    _dead_pane(tmux_server, "kill -9 $$")
    assert wait_for(lambda: read(tmux_server).panes[0].exit_signal == 9)
    (pane,) = read(tmux_server).panes
    assert pane.exit_status is None
    pane.pane_object("/tmp/x/s")  # never both exit fields


# --- no server ----------------------------------------------------------------------------------


def test_no_socket_file_means_no_server(tmux_server: TmuxServer) -> None:
    assert not os.path.exists(tmux_server.socket_path)
    assert read(tmux_server) == PaneRead(server_running=False, panes=(), warnings=())


def test_a_refused_connection_means_no_server(tmux_server: TmuxServer) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(tmux_server.socket_path)
    stale.close()
    assert read(tmux_server) == PaneRead(server_running=False, panes=(), warnings=())


def test_a_killed_server_means_no_server(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    assert tmux_server.raw("kill-server").returncode == 0
    assert os.path.exists(tmux_server.socket_path)  # kill-server leaves the socket (spike F)
    assert read(tmux_server).server_running is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the socket's permissions")
def test_permission_denied_is_server_unavailable(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    os.chmod(tmux_server.socket_path, 0)
    try:
        with pytest.raises(TermctlError) as info:
            read(tmux_server)
        assert info.value.code == "server_unavailable"
    finally:
        os.chmod(tmux_server.socket_path, 0o600)


def test_the_reader_never_starts_a_server(tmux_server: TmuxServer) -> None:
    read(tmux_server)
    assert not tmux_server.alive()
    assert not os.path.exists(tmux_server.socket_path)
