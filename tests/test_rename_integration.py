"""`rename` end to end against real tmux, through ``main`` with the system gateway, on a private
server (``tmux_server`` in conftest.py): guarded writes (spike F5), rename eligibility, the
``#`` doubling round trip, tmux's escaping of window names in the no-op comparison, forged
control bytes and dead panes (interface.md `rename`, "Names", "tmux argument rules" rule 5).

The CI ``tmux`` jobs run these on tmux 3.2a (the floor) and on the runner image's tmux.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from termctl.cli import main
from termctl.gateway import SystemGateway

from .conftest import TMUX_3_2, TmuxServer, tmux_stored_name, wait_for
from .schemas import validate
from .test_cli_read_integration import NO_CONFIG
from .test_panes_integration import ID, OTHER_ID, new_window, tag, window_of
from .test_tmux_integration import NAMES

pytestmark = pytest.mark.tmux

# Names with backslashes, which rename-window stores doubled (3.2a, 3.5a), besides the "#", ";",
# leading "-", quote, "$" and non-ASCII names of the argument builder's own tests. On tmux 3.2
# each of them with a "$" reads back with "\\$" and must still be a no-op the second time (#19).
BACKSLASH_NAMES = ["a\\b", "\\", "a\\\\b", "\\033", "x\\", "a\\$b", "$\\a"]
RENAME_NAMES = [*NAMES, *BACKSLASH_NAMES]


class Interposing(SystemGateway):
    """The system gateway, running ``before_write`` once, just before the first guarded write
    (an ``if-shell`` invocation) reaches tmux."""

    def __init__(self, before_write: Callable[[], None] | None = None) -> None:
        self.before_write = before_write

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Any:
        if "if-shell" in argv and self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        return super().run(argv, stdin=stdin, timeout=timeout, env=env)


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def termctl(server: TmuxServer, *argv: str, gateway: SystemGateway | None = None) -> Outcome:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--socket", server.socket_path, *argv],
        gateway=gateway,
        environ={**server.env, "XDG_CONFIG_HOME": NO_CONFIG},
        stdout=stdout,
        stderr=stderr,
    )
    return Outcome(code, stdout.getvalue(), stderr.getvalue())


def log(server: TmuxServer) -> list[dict[str, Any]]:
    path = server.root / ".local" / "state" / "termctl" / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def window_name(server: TmuxServer, pane_id: str) -> str:
    return server.raw("display", "-p", "-t", pane_id, "#{window_name}").stdout.removesuffix("\n")


def option(server: TmuxServer, pane_id: str, name: str = "@termctl_name") -> str:
    result = server.raw_bytes("show-options", "-p", "-q", "-v", "-t", pane_id, name)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().removesuffix("\n")


def option_bytes(server: TmuxServer, pane_id: str, name: str = "@termctl_name") -> int:
    """The stored length of an option in bytes (tmux prints control bytes escaped)."""
    return int(server.raw("display", "-p", "-t", pane_id, f"#{{n:{name}}}").stdout)


def managed(server: TmuxServer) -> str:
    """Start the server with a managed pane carrying ``ID``, alone in its window, plus an
    unmanaged session so the server outlives that pane."""
    pane_id = server.start()
    tag(server, pane_id)
    server.raw("new-session", "-d", "-s", "keep", "--", "/usr/bin/env", "--", "sleep", "600")
    return pane_id


# --- a rename -----------------------------------------------------------------------------------


def test_rename_sets_the_option_and_the_window_name(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    result = termctl(tmux_server, "--json", "rename", "abcd", "new name")
    assert (result.code, result.stderr) == (0, "")
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["id"], out["name"], out["display_name"]) == (ID, "new name", "new name")
    assert (out["changed"], out["display_updated"], out["dry_run"]) == (True, True, False)
    assert option(tmux_server, pane_id) == "new name"
    assert window_name(tmux_server, pane_id) == "new name"
    [record] = log(tmux_server)
    validate(record, "termctl/log.json")
    assert (record["event"], record["result"], record["id"]) == ("rename", "ok", ID)
    assert record["socket_path"] == tmux_server.socket_path
    assert termctl(tmux_server, "rename", ID, "other").stdout == "other\n"


def test_rename_keeps_the_marker_in_the_displayed_name(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tag(tmux_server, pane_id, **{"@termctl_marker": "attention",
                                 "@termctl_marker_text": "\U0001f514 "})  # fmt: skip
    out = termctl(tmux_server, "--json", "rename", ID, "new").json()
    assert (out["display_name"], out["marker"]) == ("\U0001f514 new", "attention")
    assert window_name(tmux_server, pane_id) == "\U0001f514 new"
    assert option(tmux_server, pane_id, "@termctl_marker_text") == "\U0001f514 "


@pytest.mark.parametrize("name", RENAME_NAMES)
def test_names_round_trip_and_renaming_again_is_a_no_op(tmux_server: TmuxServer, name: str) -> None:
    pane_id = managed(tmux_server)
    first = termctl(tmux_server, "--json", "rename", ID, "--", name)
    assert first.code == 0, first.stderr
    assert option(tmux_server, pane_id) == name
    assert window_name(tmux_server, pane_id) == tmux_stored_name(name)
    assert first.json()["display_name"] == tmux_stored_name(name)
    again = termctl(tmux_server, "--json", "rename", ID, "--", name)
    assert again.code == 0, again.stderr
    assert (again.json()["changed"], again.json()["display_updated"]) == (False, False)
    assert len(log(tmux_server)) == 1  # the no-op wrote no record


def test_a_window_named_with_new_window_n_is_a_no_op_too(tmux_server: TmuxServer) -> None:
    # open (#14) names the window with -n, which keeps a backslash as given.
    tmux_server.start()
    pane_id = new_window(tmux_server, "-n", "a\\b")
    tag(tmux_server, pane_id, **{"@termctl_name": "a\\b"})
    tmux_server.raw("new-session", "-d", "-s", "keep", "--", "/usr/bin/env", "--", "sleep", "600")
    assert window_name(tmux_server, pane_id) == "a\\b"
    result = termctl(tmux_server, "--json", "rename", ID, "a\\b")
    assert (result.code, result.json()["changed"]) == (0, False)


# --- eligibility --------------------------------------------------------------------------------


def test_a_split_window_is_never_renamed(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("rename-window", "-t", pane_id, "users-window")
    tmux_server.raw("split-window", "-d", "-t", pane_id, "--", "/usr/bin/env", "--", "sleep", "600")
    result = termctl(tmux_server, "--json", "rename", ID, "new")
    assert result.code == 0
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["changed"], out["display_updated"], out["display_name"]) == (
        True,
        False,
        "users-window",
    )
    assert option(tmux_server, pane_id) == "new"
    assert window_name(tmux_server, pane_id) == "users-window"
    # Nothing to rename and the option already set: a no-op, whatever the window is called.
    assert termctl(tmux_server, "--json", "rename", ID, "new").json()["changed"] is False


def test_a_pane_moved_to_another_window_never_renames_it(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    other = new_window(tmux_server, "-n", "elsewhere")
    tmux_server.raw("join-pane", "-d", "-s", pane_id, "-t", other)
    tmux_server.raw("kill-pane", "-t", other)  # alone again, but not in its own window
    assert window_of(tmux_server, pane_id) != option(tmux_server, pane_id, "@termctl_window")
    out = termctl(tmux_server, "--json", "rename", ID, "new").json()
    assert (out["changed"], out["display_updated"]) == (True, False)
    assert window_name(tmux_server, pane_id) == "elsewhere"
    assert option(tmux_server, pane_id) == "new"


def test_a_dead_pane_is_renamed(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    pane_id = new_window(tmux_server)
    tmux_server.raw("set", "-p", "-t", pane_id, "remain-on-exit", "on")
    tag(tmux_server, pane_id)
    tmux_server.raw("respawn-pane", "-k", "-t", pane_id, "--", "/usr/bin/env", "--", "true")
    assert wait_for(
        lambda: tmux_server.raw("display", "-p", "-t", pane_id, "#{pane_dead}").stdout == "1\n"
    )
    result = termctl(tmux_server, "--json", "rename", ID, "dead-name")
    assert result.code == 0, result.stderr
    out = result.json()
    validate(out, "termctl/cli/rename.json")
    assert (out["state"], out["display_name"], out["display_updated"]) == (
        "dead",
        "dead-name",
        True,
    )
    assert window_name(tmux_server, pane_id) == "dead-name"


# --- guarded writes -----------------------------------------------------------------------------


def test_spike_f5_a_restarted_server_reusing_the_pane_id_is_never_acted_on(
    tmux_server: TmuxServer,
) -> None:
    pane_id = managed(tmux_server)

    def restart() -> None:
        # The server restarts between the read under the lock and the write; its first pane
        # gets the same %N, as another managed pane.
        tmux_server.raw("kill-server")
        assert wait_for(lambda: not tmux_server.alive())
        assert tmux_server.start() == pane_id
        tag(tmux_server, pane_id, OTHER_ID, **{"@termctl_name": "theirs"})
        tmux_server.raw("rename-window", "-t", pane_id, "their-window")

    result = termctl(tmux_server, "--json", "rename", ID, "new", gateway=Interposing(restart))
    assert result.code == 10
    envelope = result.json()
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "not_found"
    assert option(tmux_server, pane_id) == "theirs"
    assert option(tmux_server, pane_id, "@termctl_id") == OTHER_ID
    assert window_name(tmux_server, pane_id) == "their-window"
    [record] = log(tmux_server)
    assert (record["result"], record["exit_code"], record["id"]) == ("error", 10, ID)


def test_spike_f5_an_unmanaged_pane_reusing_the_pane_id_is_never_acted_on(
    tmux_server: TmuxServer,
) -> None:
    pane_id = managed(tmux_server)

    def restart() -> None:
        tmux_server.raw("kill-server")
        assert wait_for(lambda: not tmux_server.alive())
        assert tmux_server.start() == pane_id
        tmux_server.raw("rename-window", "-t", pane_id, "plain")

    result = termctl(tmux_server, "rename", ID, "new", gateway=Interposing(restart))
    assert result.code == 10
    assert option(tmux_server, pane_id) == ""
    assert window_name(tmux_server, pane_id) == "plain"


def test_a_pane_vanishing_mid_command_exits_10(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    survivor = new_window(tmux_server, "-n", "survivor")

    def vanish() -> None:
        tmux_server.raw("kill-pane", "-t", pane_id)

    result = termctl(tmux_server, "--json", "rename", ID, "new", gateway=Interposing(vanish))
    assert result.code == 10
    assert result.json()["error"]["code"] == "not_found"
    assert window_name(tmux_server, survivor) == "survivor"


def test_a_window_split_mid_command_gets_the_option_only(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("rename-window", "-t", pane_id, "before")

    def split() -> None:
        tmux_server.raw(
            "split-window", "-d", "-t", pane_id, "--", "/usr/bin/env", "--", "sleep", "600"
        )

    result = termctl(tmux_server, "--json", "rename", ID, "new", gateway=Interposing(split))
    assert result.code == 0, result.stderr
    assert (result.json()["changed"], result.json()["display_updated"]) == (True, False)
    assert option(tmux_server, pane_id) == "new"
    assert window_name(tmux_server, pane_id) == "before"


# --- forged control bytes -----------------------------------------------------------------------


@pytest.mark.skipif(TMUX_3_2, reason="tmux 3.3+ prints the byte escaped; 3.2: the next test")
def test_a_forged_control_byte_is_replaced_not_taken_for_the_name(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    # Someone with socket access stores "n" ESC "x"; tmux prints it as the text n\033x.
    tmux_server.raw("set", "-p", "-t", pane_id, "@termctl_name", "n\x1bx")
    tmux_server.raw("rename-window", "-t", pane_id, "n\\033x")
    assert option(tmux_server, pane_id) == "n\\033x" and option_bytes(tmux_server, pane_id) == 3
    shown = termctl(tmux_server, "--json", "show", ID).json()
    assert shown["name"] == "n\\033x"
    result = termctl(tmux_server, "--json", "rename", ID, "n\\033x")
    assert (result.code, result.json()["changed"]) == (0, True)
    assert option_bytes(tmux_server, pane_id) == 6
    # Now the literal text is stored: renaming to it again is a no-op.
    assert termctl(tmux_server, "--json", "rename", ID, "n\\033x").json()["changed"] is False


@pytest.mark.skipif(not TMUX_3_2, reason="tmux 3.2 prints the byte raw; 3.3+: the previous test")
def test_a_forged_control_byte_makes_the_pane_ignored_on_tmux_3_2(tmux_server: TmuxServer) -> None:
    # tmux 3.2 prints a raw ESC in an option as it is, so the name breaks the grammar and the
    # pane is ignored with a warning (interface.md "Residual risks", forged control bytes).
    pane_id = managed(tmux_server)
    tmux_server.raw("set", "-p", "-t", pane_id, "@termctl_name", "n\x1bx")
    assert option(tmux_server, pane_id) == "n\x1bx"
    result = termctl(tmux_server, "--json", "rename", ID, "n\\033x")
    assert result.code == 10
    assert f"ignoring managed pane {pane_id}" in result.stderr and "\x1b" not in result.stderr
    assert option_bytes(tmux_server, pane_id) == 3  # never rewritten


def test_a_forged_control_byte_in_the_window_name_is_replaced(tmux_server: TmuxServer) -> None:
    # new-window -n keeps a raw ESC, which tmux 3.3+ prints as the text \033: the window name
    # reads like the recomputed name but is not it. tmux 3.2 prints it raw.
    tmux_server.start()
    pane_id = new_window(tmux_server, "-n", "n\x1bx")
    tag(tmux_server, pane_id, **{"@termctl_name": "n\\033x"})
    tmux_server.raw("new-session", "-d", "-s", "keep", "--", "/usr/bin/env", "--", "sleep", "600")
    assert window_name(tmux_server, pane_id) == ("n\x1bx" if TMUX_3_2 else "n\\033x")
    assert option_bytes(tmux_server, pane_id, "window_name") == 3
    result = termctl(tmux_server, "--json", "rename", ID, "n\\033x")
    assert (result.code, result.json()["changed"]) == (0, True)
    assert option_bytes(tmux_server, pane_id, "window_name") > 3
    assert termctl(tmux_server, "--json", "rename", ID, "n\\033x").json()["changed"] is False


@pytest.mark.parametrize("separator", ["\t", "\n"], ids=["tab", "newline"])
def test_a_tab_or_newline_in_the_window_name_is_not_taken_for_a_space(
    tmux_server: TmuxServer, separator: str
) -> None:
    # The reader shows a tab or newline in a window name as a space; new-window -n keeps it.
    tmux_server.start()
    pane_id = new_window(tmux_server, "-n", f"a{separator}b")
    tag(tmux_server, pane_id, **{"@termctl_name": "a b"})
    tmux_server.raw("new-session", "-d", "-s", "keep", "--", "/usr/bin/env", "--", "sleep", "600")
    assert termctl(tmux_server, "--json", "show", ID).json()["display_name"] == "a b"
    result = termctl(tmux_server, "--json", "rename", ID, "a b")
    assert (result.code, result.json()["changed"]) == (0, True), result.stderr
    assert window_name(tmux_server, pane_id) == "a b"
    assert termctl(tmux_server, "--json", "rename", ID, "a b").json()["changed"] is False


def test_raw_bytes_never_reach_human_output(tmux_server: TmuxServer) -> None:
    pane_id = managed(tmux_server)
    tmux_server.raw("set", "-p", "-t", pane_id, "@termctl_name", "n\x1bx")
    result = termctl(tmux_server, "rename", ID, "plain")
    # tmux 3.2 prints the ESC raw: the pane is ignored, and the warning shows the byte escaped.
    assert result.code == (10 if TMUX_3_2 else 0)
    assert "\x1b" not in result.stdout + result.stderr
    if TMUX_3_2:
        assert "'n\\x1bx'" in result.stderr


# --- the lock directory ---------------------------------------------------------------------------


def test_rename_creates_a_private_lock_file(tmux_server: TmuxServer) -> None:
    managed(tmux_server)
    assert termctl(tmux_server, "rename", ID, "new").code == 0
    locks = tmux_server.root / ".local" / "state" / "termctl" / "locks"
    [lock_file] = list(locks.iterdir())
    assert lock_file.stat().st_mode & 0o777 == 0o600
    assert Path(locks).stat().st_mode & 0o777 == 0o700
