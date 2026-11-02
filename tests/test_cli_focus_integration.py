"""`focus` and `open --focus` end to end against real tmux, on a private server
(``tmux_server`` in conftest.py) with clients this test starts on pseudo-terminals it owns.
Spike D2, F1 and F7 regressions (design.md "Live verification").

No test touches a user's tmux: every client attaches to the fixture's private socket with the
fixture's environment (private ``HOME`` and ``XDG_CONFIG_HOME``), the server is started with
``-f /dev/null``, and teardown stops exactly that server with ``kill-server``; a client still
running after that is killed by the exact process this test started.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import pty
import struct
import subprocess
import sys
import termios
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from termctl.cli import main
from termctl.gateway import SystemGateway

from .conftest import NO_CONFIG, TMUX_VERSION, TmuxServer, wait_for
from .schemas import validate

pytestmark = pytest.mark.tmux

AGENT = "/tmp/termctl-test-agent.sock"

# TODO(#25): tmux 3.2 differs from 3.3+ where these tests expect 3.5a's behaviour: it reports a
# "$" in a session name as "\\$" (as for window names, test_tmux_integration.py). #25 covers
# tmux 3.2.
XFAIL_3_2_DOLLAR = pytest.mark.xfail(
    TMUX_VERSION is not None and TMUX_VERSION < (3, 3),
    reason="#25: tmux 3.2 reports '$' as '\\$' in session_name",
    strict=True,
)


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


class TerminalGateway(SystemGateway):
    """The system gateway reporting stdin and stderr as terminals, so ``--json`` takes step 2
    without a real terminal (the exec path is tested in a child process below)."""

    def stdin_isatty(self) -> bool:
        return True

    def stderr_isatty(self) -> bool:
        return True


def termctl(
    server: TmuxServer,
    *argv: str,
    environ: dict[str, str] | None = None,
    gateway: SystemGateway | None = None,
) -> Outcome:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--socket", server.socket_path, *argv],
        gateway=gateway,
        environ={**server.env, "XDG_CONFIG_HOME": NO_CONFIG, **(environ or {})},
        stdout=stdout,
        stderr=stderr,
    )
    return Outcome(code, stdout.getvalue(), stderr.getvalue())


@dataclass
class Terminal:
    """A program this test runs on a pseudo-terminal it owns (a tmux client, or termctl)."""

    proc: subprocess.Popen[bytes]
    master: int
    output: bytearray = field(default_factory=bytearray)


@dataclass
class Terminals:
    server: TmuxServer
    started: list[Terminal] = field(default_factory=list[Terminal])

    def run(self, argv: list[str], env: dict[str, str]) -> Terminal:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
        )
        os.close(slave)
        terminal = Terminal(proc, master)
        threading.Thread(target=_drain, args=(terminal,), daemon=True).start()
        self.started.append(terminal)
        return terminal

    def client(self, target: str, **env: str) -> str:
        """Attach a client to ``target`` (with -E, so it imports nothing into the session);
        returns its name once the server lists it."""
        before = set(client_names(self.server))
        self.run(
            [self.server.tmux_bin, "-S", self.server.socket_path, "attach", "-E", "-t", target],
            {**self.server.env, "TERM": "xterm", **env},
        )
        assert wait_for(lambda: len(set(client_names(self.server)) - before) == 1)
        (name,) = set(client_names(self.server)) - before
        return name

    def close(self) -> None:
        self.server.raw("kill-server")
        for terminal in self.started:
            try:
                terminal.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                terminal.proc.kill()  # the exact process this test started
                terminal.proc.wait(timeout=10)
            os.close(terminal.master)


def _drain(terminal: Terminal) -> None:
    try:
        while chunk := os.read(terminal.master, 4096):
            terminal.output.extend(chunk)
    except OSError:
        pass


@pytest.fixture
def terminals(tmux_server: TmuxServer) -> Iterator[Terminals]:
    found = Terminals(tmux_server)
    try:
        yield found
    finally:
        found.close()


def client_names(server: TmuxServer) -> list[str]:
    return server.raw("list-clients", "-F", "#{client_name}").stdout.split()


def client_view(server: TmuxServer, name: str) -> tuple[str, str, str]:
    """The session id, window id and pane id client ``name`` shows."""
    for line in server.raw(
        "list-clients", "-F", "#{client_name} #{session_id} #{window_id} #{pane_id}"
    ).stdout.splitlines():
        client, session, window, pane = line.split()
        if client == name:
            return session, window, pane
    raise AssertionError(f"no client {name}")


def fmt(server: TmuxServer, target: str, text: str) -> str:
    result = server.raw("display", "-p", "-t", target, text)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def opened(server: TmuxServer, *argv: str) -> Any:
    result = termctl(server, "--json", "open", *argv, "--", "sleep", "600")
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/open.json")
    return out


def focused(server: TmuxServer, *argv: str, **kwargs: Any) -> Any:
    result = termctl(server, "--json", "focus", *argv, **kwargs)
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/focus.json")
    return out


def tmux_env(server: TmuxServer, session: str) -> str:
    """``TMUX`` as tmux sets it in a pane of ``session``: socket, server pid, session id."""
    return f"{server.socket_path},1,{fmt(server, session, '#{session_id}').removeprefix('$')}"


def session_has(server: TmuxServer, session_id: str, entry: str) -> bool:
    return entry in server.raw("show-environment", "-t", session_id).stdout.splitlines()


# --- switching (step 3, --client) ---------------------------------------------------------------


def test_switches_the_client_to_the_pane(tmux_server: TmuxServer, terminals: Terminals) -> None:
    tmux_server.start("base")
    pane = opened(tmux_server, "--session", "work")
    client = terminals.client("=base:")
    out = focused(tmux_server, pane["id"], "--no-attach")
    assert (out["action"], out["client"]) == ("switched", client)
    session_id = fmt(tmux_server, pane["pane_id"], "#{session_id}")
    assert client_view(tmux_server, client) == (session_id, pane["window_id"], pane["pane_id"])


@pytest.mark.parametrize("name", ["my work", pytest.param("my $work", marks=XFAIL_3_2_DOLLAR)])
def test_a_session_the_user_renamed_is_focused(
    name: str, tmux_server: TmuxServer, terminals: Terminals
) -> None:
    """#9's note: a session renamed to a name outside termctl's session grammar."""
    tmux_server.start("base")
    pane = opened(tmux_server, "--session", "work")
    assert tmux_server.raw("rename-session", "-t", "=work", name).returncode == 0
    client = terminals.client("=base:")
    out = focused(tmux_server, pane["id"], "--no-attach")
    assert out["session"] == name
    assert client_view(tmux_server, client)[2] == pane["pane_id"]


def test_client_switches_the_named_client_only(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    tmux_server.start("base")
    pane = opened(tmux_server, "--session", "work")
    first = terminals.client("=base:")
    second = terminals.client("=base:")
    out = focused(tmux_server, pane["id"], "--client", first)
    assert out["client"] == first
    assert client_view(tmux_server, first)[2] == pane["pane_id"]
    assert client_view(tmux_server, second)[0] == fmt(tmux_server, "=base:", "#{session_id}")


def test_a_client_that_is_not_attached_is_client_not_found(tmux_server: TmuxServer) -> None:
    pane = opened(tmux_server)
    result = termctl(tmux_server, "--json", "focus", pane["id"], "--client", "/dev/pts/999")
    assert result.code == 10
    assert result.json()["error"]["code"] == "client_not_found"


def test_no_client_is_exit_12(tmux_server: TmuxServer) -> None:
    pane = opened(tmux_server)
    result = termctl(tmux_server, "--json", "focus", pane["id"], "--no-attach")
    assert result.code == 12
    assert result.json()["error"]["code"] == "no_client"


def test_dry_run_switches_nothing(tmux_server: TmuxServer, terminals: Terminals) -> None:
    tmux_server.start("base")
    pane = opened(tmux_server, "--session", "work")
    client = terminals.client("=base:")
    before = client_view(tmux_server, client)
    out = focused(tmux_server, pane["id"], "--no-attach", "--dry-run")
    assert (out["client"], out["dry_run"]) == (client, True)
    assert client_view(tmux_server, client) == before


# --- step 1 -------------------------------------------------------------------------------------


def test_step_1_switches_only_a_client_on_the_callers_session(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    caller_pane = tmux_server.start("mine")
    tmux_server.raw("new-session", "-d", "-s", "theirs", "--", "/usr/bin/env", "--", "sleep", "600")
    pane = opened(tmux_server, "--session", "work")
    mine = terminals.client("=mine:")
    theirs = terminals.client("=theirs:")  # attached later: the more recent one
    theirs_view = client_view(tmux_server, theirs)
    inside = {"TMUX": tmux_env(tmux_server, "=mine:"), "TMUX_PANE": caller_pane}
    out = focused(tmux_server, pane["id"], environ=inside)
    assert out["client"] == mine
    assert client_view(tmux_server, mine)[2] == pane["pane_id"]
    assert client_view(tmux_server, theirs) == theirs_view


def test_step_1_with_grouped_sessions_switches_the_client_on_the_session_tmux_names(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    """The caller's pane is in "mine" and in "grouped" (one window group); tmux would report
    either for the pane, so the caller's session comes from the third field of TMUX."""
    caller_pane = tmux_server.start("mine")
    assert tmux_server.raw("new-session", "-d", "-t", "=mine:", "-s", "grouped").returncode == 0
    pane = opened(tmux_server, "--session", "work")
    mine = terminals.client("=mine:")
    grouped = terminals.client("=grouped:")
    grouped_view = client_view(tmux_server, grouped)
    inside = {"TMUX": tmux_env(tmux_server, "=mine:"), "TMUX_PANE": caller_pane}
    assert focused(tmux_server, pane["id"], environ=inside)["client"] == mine
    assert client_view(tmux_server, grouped) == grouped_view
    mine_view = client_view(tmux_server, mine)
    inside = {"TMUX": tmux_env(tmux_server, "=grouped:"), "TMUX_PANE": caller_pane}
    assert focused(tmux_server, pane["id"], environ=inside)["client"] == grouped
    assert client_view(tmux_server, grouped)[2] == pane["pane_id"]
    assert client_view(tmux_server, mine) == mine_view


# --- spike F1: -E keeps the client's environment out of the session -----------------------------


def test_f1_switching_with_e_leaves_the_session_environment_unchanged(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    tmux_server.start("base")
    tmux_server.raw("set", "-g", "update-environment", "SSH_AUTH_SOCK")
    pane = opened(tmux_server, "--session", "work")
    control = opened(tmux_server, "--session", "control")
    session_id = fmt(tmux_server, pane["pane_id"], "#{session_id}")
    control_id = fmt(tmux_server, control["pane_id"], "#{session_id}")
    client = terminals.client("=base:", SSH_AUTH_SOCK=AGENT)
    # Control: a switch without -E imports the client's SSH_AUTH_SOCK, so the check can fail.
    assert tmux_server.raw("switch-client", "-c", client, "-t", control_id).returncode == 0
    assert session_has(tmux_server, control_id, f"SSH_AUTH_SOCK={AGENT}")
    focused(tmux_server, pane["id"], "--client", client)
    assert client_view(tmux_server, client)[2] == pane["pane_id"]
    assert not session_has(tmux_server, session_id, f"SSH_AUTH_SOCK={AGENT}")


def test_f1_d2_the_json_attach_argv_attaches_to_the_pane_with_e(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    tmux_server.raw("set", "-g", "update-environment", "SSH_AUTH_SOCK")
    pane = opened(tmux_server, "--session", "work")
    opened(tmux_server, "--session", "work")  # a later window: the pane's is not current
    session_id = fmt(tmux_server, pane["pane_id"], "#{session_id}")
    out = focused(tmux_server, pane["id"], gateway=TerminalGateway())
    assert out["action"] == "attach"
    assert os.path.isabs(out["argv"][0])
    assert os.path.realpath(out["argv"][0]) == tmux_server.tmux_bin
    assert out["argv"][1:] == [
        "-S", tmux_server.socket_path, "attach", "-E",
        "-t", f"={session_id}:{pane['window_id']}.{pane['pane_id']}",
    ]  # fmt: skip
    # The caller execs it with its own environment.
    terminals.run(out["argv"], {**tmux_server.env, "TERM": "xterm", "SSH_AUTH_SOCK": AGENT})
    assert wait_for(lambda: len(client_names(tmux_server)) == 1)
    (client,) = client_names(tmux_server)
    assert client_view(tmux_server, client) == (session_id, pane["window_id"], pane["pane_id"])
    assert not session_has(tmux_server, session_id, f"SSH_AUTH_SOCK={AGENT}")


# --- spike F7: the attach target enforces containment -------------------------------------------


def test_f7_a_stale_attach_argv_fails_instead_of_attaching_elsewhere(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    pane = opened(tmux_server, "--session", "work")
    out = focused(tmux_server, pane["id"], gateway=TerminalGateway())
    # The pane moves into another window (spike D5): the argv's window is gone.
    other = opened(tmux_server, "--session", "work")
    moved = tmux_server.raw("join-pane", "-d", "-s", pane["pane_id"], "-t", other["pane_id"])
    assert moved.returncode == 0, moved.stderr
    assert fmt(tmux_server, pane["pane_id"], "#{window_id}") == other["window_id"]
    attach = terminals.run(out["argv"], {**tmux_server.env, "TERM": "xterm"})
    assert attach.proc.wait(timeout=10) != 0
    assert client_names(tmux_server) == []
    assert b"can't find" in attach.output


# --- step 2 in human mode: termctl replaces itself with the attach ------------------------------


def test_human_focus_from_a_terminal_execs_the_attach(
    tmux_server: TmuxServer, terminals: Terminals, tmp_path: Path
) -> None:
    pane = opened(tmux_server, "--session", "work")
    opened(tmux_server, "--session", "work")
    state = tmp_path / "state"
    env = {
        **tmux_server.env,
        "TERM": "xterm",
        "XDG_CONFIG_HOME": NO_CONFIG,
        "XDG_STATE_HOME": str(state),
        "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
    }
    argv = [sys.executable, "-m", "termctl", "--socket", tmux_server.socket_path, "focus",
            pane["id"]]  # fmt: skip
    termctl_run = terminals.run(argv, env)
    assert wait_for(lambda: len(client_names(tmux_server)) == 1, timeout=15)
    (client,) = client_names(tmux_server)
    assert client_view(tmux_server, client)[1:] == (pane["window_id"], pane["pane_id"])
    # The client is termctl's own process, replaced by tmux.
    pids = tmux_server.raw("list-clients", "-F", "#{client_pid}").stdout.split()
    assert pids == [str(termctl_run.proc.pid)]
    log = (state / "termctl" / "log.jsonl").read_text(encoding="utf-8")
    [record] = [json.loads(line) for line in log.splitlines()]
    validate(record, "termctl/log.json")
    assert (record["event"], record["action"], record["result"]) == ("focus", "attach", "ok")


# --- open --focus -------------------------------------------------------------------------------


def test_open_focus_switches_a_client_and_reports_focused(
    tmux_server: TmuxServer, terminals: Terminals
) -> None:
    tmux_server.start("base")
    client = terminals.client("=base:")
    out = opened(tmux_server, "--session", "work", "--focus")
    assert out["focused"] is True
    assert client_view(tmux_server, client)[2] == out["pane_id"]


def test_open_focus_without_a_client_reports_not_focused(tmux_server: TmuxServer) -> None:
    result = termctl(tmux_server, "--json", "open", "--focus", "--", "sleep", "600")
    assert result.code == 0, result.stderr
    assert result.json()["focused"] is False
    assert "no tmux client" in result.stderr
