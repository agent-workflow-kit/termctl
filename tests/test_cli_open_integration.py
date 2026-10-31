"""`open` end to end against real tmux: the whole open order through ``main`` with the system
gateway, on a private server (``tmux_server`` in conftest.py). Spike C1, C7, D3, E2, E5 and
G1 to G4 regressions (design.md "Live verification").

The caller's environment is the fixture's (``HOME`` is its private directory), so a server
``open`` starts reads only the config a test writes there, never the user's. Teardown stops
exactly the server on the fixture's socket. Reading a pane's environment uses
``/proc/<pid>/environ`` (Linux).
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from termctl.cli import main

from .conftest import TmuxServer, wait_for
from .schemas import validate

pytestmark = pytest.mark.tmux

NO_CONFIG = str(Path(__file__).parent / "no-config")
PROC = Path("/proc/self/environ").exists()
needs_proc = pytest.mark.skipif(not PROC, reason="reads /proc/<pid>/environ")
SESSION = "termctl"


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


def termctl(server: TmuxServer, *argv: str, environ: dict[str, str] | None = None) -> Outcome:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--socket", server.socket_path, *argv],
        environ={**server.env, "XDG_CONFIG_HOME": NO_CONFIG, **(environ or {})},
        stdout=stdout,
        stderr=stderr,
    )
    return Outcome(code, stdout.getvalue(), stderr.getvalue())


def opened(server: TmuxServer, *argv: str, environ: dict[str, str] | None = None) -> Any:
    result = termctl(server, "--json", "open", *argv, environ=environ)
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/open.json")
    return out


def fmt(server: TmuxServer, target: str, text: str) -> str:
    result = server.raw("display", "-p", "-t", target, text)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def pane_exists(server: TmuxServer, pane_id: str) -> bool:
    # `display -t` with a missing pane exits 0 with empty fields (spike E6).
    return server.raw("list-panes", "-t", pane_id).returncode == 0


def environ_of(server: TmuxServer, pane_id: str) -> dict[str, str]:
    pid = fmt(server, pane_id, "#{pane_pid}")
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(entry.decode().split("=", 1) for entry in raw.split(b"\0") if entry)


def session_env(server: TmuxServer, session: str = SESSION) -> list[str]:
    return server.raw("show-environment", "-t", f"={session}:").stdout.splitlines()


def global_env(server: TmuxServer) -> list[str]:
    return server.raw("show-environment", "-g").stdout.splitlines()


# --- the pane: tagged before its command can exit -----------------------------------------------


@pytest.mark.parametrize(("command", "status"), [("true", 0), ("false", 1)])
def test_c1_e5_a_fast_exiting_command_is_tagged_and_no_placeholder_is_left(
    tmux_server: TmuxServer, command: str, status: int
) -> None:
    out = opened(tmux_server, "--keep-on-exit", "always", "--", command)
    pane = out["pane_id"]
    assert wait_for(lambda: fmt(tmux_server, pane, "#{pane_dead}") == "1")
    assert fmt(tmux_server, pane, "#{pane_dead_status}") == str(status)
    # The options were set before the command ran, so its dead pane is managed.
    assert fmt(tmux_server, pane, "#{@termctl_pane} #{@termctl_id}") == f"{pane} {out['id']}"
    # The placeholder was replaced, not left beside the command.
    assert tmux_server.raw("list-panes", "-t", out["window_id"]).stdout.count("\n") == 1
    assert "sleep" not in fmt(tmux_server, pane, "#{pane_start_command}")
    shown = termctl(tmux_server, "--json", "show", out["id"]).json()
    assert (shown["state"], shown["exit_status"]) == ("dead", status)


def test_c1_keep_never_closes_the_window_and_reports_gone(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    out = opened(tmux_server, "--", "true")
    # `true` has normally exited and taken its window before the read after tagging (always
    # "gone" on 3.5a); a slow host may still read it running, and tmux 3.2 may briefly show it
    # dead with neither exit field set (interface.md "Command exit", spike D; review round 1,
    # R1-10). Any other dead state is wrong.
    assert out["state"] in ("gone", "running", "dead")
    if out["state"] in ("gone", "dead"):
        assert (out["pid"], out["exit_status"], out["exit_signal"]) == (None, None, None)
    assert wait_for(lambda: not pane_exists(tmux_server, out["pane_id"]))
    assert termctl(tmux_server, "exists", out["id"]).code == 10


def test_keep_failed_closes_on_success_and_keeps_a_failure(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    ok = opened(tmux_server, "--keep-on-exit", "failed", "--", "true")
    failed = opened(tmux_server, "--keep-on-exit", "failed", "--", "false")
    assert wait_for(lambda: not pane_exists(tmux_server, ok["pane_id"]))
    assert wait_for(lambda: fmt(tmux_server, failed["pane_id"], "#{pane_dead}") == "1")
    assert fmt(tmux_server, failed["pane_id"], "#{@termctl_keep}") == "failed"


def test_a_running_pane_is_reported_with_every_option(tmux_server: TmuxServer) -> None:
    out = opened(
        tmux_server, "--name", "PR#42 review;", "--label", "kind=review", "--label", "a=b",
        "--cwd", str(tmux_server.root), "--", "sleep", "60",
    )  # fmt: skip
    assert out["state"] == "running"
    assert (out["name"], out["display_name"]) == ("PR#42 review;", "PR#42 review;")
    assert (out["labels"], out["placement"], out["session"]) == (["a=b", "kind=review"],
                                                                "default", SESSION)  # fmt: skip
    pane = out["pane_id"]
    assert fmt(tmux_server, pane, "#{pane_current_path}") == str(tmux_server.root)
    assert fmt(tmux_server, pane, "#{automatic-rename} #{allow-rename}") == "0 0"
    assert fmt(tmux_server, pane, "#{remain-on-exit}") == "off"
    listed = termctl(tmux_server, "--json", "list").json()["panes"]
    assert [p["id"] for p in listed] == [out["id"]]


def test_the_window_is_always_detached(tmux_server: TmuxServer) -> None:
    tmux_server.start(SESSION)
    before = fmt(tmux_server, f"={SESSION}:", "#{window_id}")
    out = opened(tmux_server, "--", "sleep", "60")
    assert out["window_id"] != before
    assert fmt(tmux_server, f"={SESSION}:", "#{window_id}") == before


def test_argv_reaches_the_program_unchanged(tmux_server: TmuxServer) -> None:
    target = tmux_server.root / "args"
    script = 'for a in "$@"; do printf "%s\\n" "$a"; done > "$0"'
    hostile = ["a;", "#{pane_id}", "-x", "'", "$HOME", "x y", "\\"]
    opened(tmux_server, "--", "sh", "-c", script, str(target), *hostile)
    assert wait_for(target.exists)
    assert wait_for(lambda: target.read_text().splitlines() == hostile)


# --- the no-argv path ---------------------------------------------------------------------------


@needs_proc
def test_e2_without_argv_the_pane_runs_a_login_shell(tmux_server: TmuxServer) -> None:
    out = opened(tmux_server)
    assert out["name"] == "shell"
    pid = fmt(tmux_server, out["pane_id"], "#{pane_pid}")
    argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
    assert argv0.startswith(b"-"), argv0


@needs_proc
@pytest.mark.parametrize("server_running", [False, True], ids=["new-session", "new-window"])
def test_g1_g3_without_argv_env_reaches_only_the_first_pane(
    tmux_server: TmuxServer, server_running: bool
) -> None:
    if server_running:
        tmux_server.start(SESSION)
    first = opened(tmux_server, "--env", "FOO=bar baz;")
    assert environ_of(tmux_server, first["pane_id"])["FOO"] == "bar baz;"
    # Spike G3c: new-session -e is removed from the session environment at once.
    assert not [line for line in session_env(tmux_server) if line.startswith("FOO=")]
    later = opened(tmux_server)
    assert "FOO" not in environ_of(tmux_server, later["pane_id"])


def later_window(server: TmuxServer, session: str) -> str:
    """A window another tmux user opens later in ``session``; its pane id."""
    result = server.raw(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={session}:",
        "--", "/usr/bin/env", "--", "sleep", "60",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@needs_proc
def test_r1_2_a_new_sessions_env_variable_missing_from_the_filter_stays_removed(
    tmux_server: TmuxServer,
) -> None:
    # Review round 1, R1-2: with set-environment -u, later windows inherited the server's
    # global SSH_AUTH_SOCK; the removal marker keeps it out of them.
    tmux_server.env["SSH_AUTH_SOCK"] = "/global/agent"
    tmux_server.start()
    del tmux_server.env["SSH_AUTH_SOCK"]
    assert "SSH_AUTH_SOCK=/global/agent" in global_env(tmux_server)
    first = opened(tmux_server, "--session", "s2", "--env", "SSH_AUTH_SOCK=/mine")
    assert environ_of(tmux_server, first["pane_id"])["SSH_AUTH_SOCK"] == "/mine"
    assert "-SSH_AUTH_SOCK" in session_env(tmux_server, "s2")
    later = later_window(tmux_server, "s2")
    assert "SSH_AUTH_SOCK" not in environ_of(tmux_server, later)


@needs_proc
def test_r1_2_a_new_sessions_env_variable_in_the_filter_gets_the_filtered_value(
    tmux_server: TmuxServer,
) -> None:
    tmux_server.start()
    first = opened(
        tmux_server, "--session", "s2", "--env", "TZ=Mine/Option", environ={"TZ": "Filtered/Zone"}
    )
    assert environ_of(tmux_server, first["pane_id"])["TZ"] == "Mine/Option"
    assert "TZ=Filtered/Zone" in session_env(tmux_server, "s2")
    later = later_window(tmux_server, "s2")
    assert environ_of(tmux_server, later)["TZ"] == "Filtered/Zone"


# --- --env with argv ----------------------------------------------------------------------------


@needs_proc
@pytest.mark.parametrize("server_running", [False, True], ids=["new-session", "new-window"])
def test_g1_g2_env_reaches_only_the_respawned_pane(
    tmux_server: TmuxServer, server_running: bool
) -> None:
    if server_running:
        tmux_server.start(SESSION)
    first = opened(tmux_server, "--env", "FOO=v1", "--env", "BAR=", "--", "sleep", "60")
    env = environ_of(tmux_server, first["pane_id"])
    assert (env["FOO"], env["BAR"]) == ("v1", "")
    assert not [line for line in session_env(tmux_server) if line.startswith(("FOO", "BAR"))]
    assert not [line for line in global_env(tmux_server) if line.startswith(("FOO", "BAR"))]
    later = opened(tmux_server, "--", "sleep", "60")
    assert "FOO" not in environ_of(tmux_server, later["pane_id"])


@needs_proc
def test_g4_env_path_reaches_the_program_through_env(tmux_server: TmuxServer) -> None:
    tools = tmux_server.root / "tools"
    tools.mkdir()
    custom = f"{tools}:/usr/bin:/bin"
    out = opened(tmux_server, "--env", f"PATH={custom}", "--", "sleep", "60")
    assert environ_of(tmux_server, out["pane_id"])["PATH"] == custom
    # The server, started by this open, keeps the filtered PATH.
    assert f"PATH={tmux_server.env['PATH']}" in global_env(tmux_server)


def test_g4_env_path_decides_where_argv0_is_found(tmux_server: TmuxServer) -> None:
    tools = tmux_server.root / "tools"
    tools.mkdir()
    tool = tools / "only-here"
    marker = tmux_server.root / "ran"
    tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
    tool.chmod(0o755)
    result = termctl(tmux_server, "--json", "open", "--", "only-here")
    assert (result.code, result.json()["error"]["code"]) == (12, "command_not_found")
    opened(tmux_server, "--env", f"PATH={tools}:/usr/bin:/bin", "--", "only-here")
    assert wait_for(marker.exists)


# --- the filtered environment -------------------------------------------------------------------

CREDENTIALS = {
    "AWS_SECRET_ACCESS_KEY": "aws-credential",
    "GITHUB_TOKEN": "gh-credential",
    "SSH_AUTH_SOCK": "/tmp/agent-credential",
    "DISPLAY_EXTRA": "display-credential",
}


@needs_proc
def test_c7_d3_a_cron_like_callers_credentials_never_reach_tmux(tmux_server: TmuxServer) -> None:
    out = opened(tmux_server, "--", "sleep", "60", environ=CREDENTIALS)
    pane_env = environ_of(tmux_server, out["pane_id"])
    server_env = "\n".join(global_env(tmux_server) + session_env(tmux_server))
    for name, value in CREDENTIALS.items():
        assert name not in pane_env
        assert value not in server_env
    # What the filter keeps does reach the server and the pane.
    assert f"HOME={tmux_server.root}" in global_env(tmux_server)
    assert pane_env["HOME"] == str(tmux_server.root)
    assert pane_env["PATH"] == tmux_server.env["PATH"]


@needs_proc
def test_d3_a_session_created_without_ssh_auth_sock_never_gives_it_to_panes(
    tmux_server: TmuxServer,
) -> None:
    # The server (started by someone else) has SSH_AUTH_SOCK globally; the caller has it too,
    # but the default server_env_allow drops it, so the new session records its removal.
    tmux_server.start()
    tmux_server.raw("set-environment", "-g", "SSH_AUTH_SOCK", "/tmp/server-agent")
    out = opened(tmux_server, "--", "sleep", "60", environ={"SSH_AUTH_SOCK": "/tmp/caller"})
    assert "SSH_AUTH_SOCK" not in environ_of(tmux_server, out["pane_id"])
    assert "-SSH_AUTH_SOCK" in session_env(tmux_server)


def test_a_started_server_reads_the_config_at_the_filtered_home(tmux_server: TmuxServer) -> None:
    (tmux_server.root / ".tmux.conf").write_text("set -g @from_home_config yes\n")
    opened(tmux_server, "--", "sleep", "60")
    assert tmux_server.raw("show", "-gv", "@from_home_config").stdout.strip() == "yes"


def test_server_env_allow_decides_the_started_servers_environment(
    tmux_server: TmuxServer,
) -> None:
    # HOME stays allowed: without it tmux would find the real user's config through passwd.
    config = tmux_server.root / "termctl.yaml"
    config.write_text('server_env_allow: [PATH, HOME, LANG, "LC_*"]\n')
    result = termctl(tmux_server, "--json", "--config", str(config), "open", "--", "sleep", "60")
    assert result.code == 0, result.stdout + result.stderr
    names = {line.split("=", 1)[0] for line in global_env(tmux_server)}
    assert {"PATH", "HOME", "LANG"} <= names
    assert not names & {"SHELL", "XDG_CONFIG_HOME"}


# --- --verbose ----------------------------------------------------------------------------------


def test_verbose_shows_env_names_never_values(tmux_server: TmuxServer) -> None:
    result = termctl(
        tmux_server, "--verbose", "--json", "open", "--env", "PATH=/usr/bin:/bin:/s3cret-dir",
        "--env", "NOT_SECRET_SAFE=s3cret-value", "--", "sleep", "60",
    )  # fmt: skip
    assert result.code == 0, result.stderr
    assert "s3cret" not in result.stderr + result.stdout
    assert "NOT_SECRET_SAFE" in result.stderr and "respawn-pane" in result.stderr


def test_verbose_without_argv_shows_env_names_never_values(tmux_server: TmuxServer) -> None:
    result = termctl(tmux_server, "--verbose", "open", "--env", "A=s3cret-value")
    assert result.code == 0, result.stderr
    assert "s3cret" not in result.stderr + result.stdout
    assert "set-environment" in result.stderr


# --- failure ------------------------------------------------------------------------------------


def test_a_forged_global_ownership_marker_fails_the_tagging(tmux_server: TmuxServer) -> None:
    # Residual risk "Forged options": a global @termctl_pane makes every open fail its guard.
    tmux_server.start(SESSION)
    tmux_server.raw("set", "-g", "@termctl_pane", "%999")
    result = termctl(tmux_server, "--json", "open", "--", "sleep", "60")
    assert result.code == 12, result.stderr
    error = result.json()
    validate(error, "termctl/cli/error.json")
    assert error["error"]["code"] == "tag_failed"
    assert termctl(tmux_server, "--json", "list").json()["panes"] == []
    # Review round 1, R1-7: the cleanup guard cannot match either, so the window stays (its
    # placeholder ends within 30 s) and a warning names it.
    assert "warning:" in result.stderr and "unmanaged" in result.stderr


def test_r1_4_a_failing_inner_command_of_the_tagging_removes_the_pane(
    tmux_server: TmuxServer,
) -> None:
    # A real failure inside the guarded chain: a command alias makes respawn-pane, the last
    # inner command, fail. @termctl_pane is already set to the pane's own id, so the cleanup
    # guard matches and the pane is killed.
    base = tmux_server.start(SESSION)
    alias = tmux_server.raw("set", "-s", "command-alias[100]", "respawn-pane=display -t %999 x")
    assert alias.returncode == 0, alias.stderr
    result = termctl(tmux_server, "--json", "open", "--", "sleep", "60")
    assert result.code == 12, result.stdout + result.stderr
    error = result.json()
    validate(error, "termctl/cli/error.json")
    assert error["error"]["code"] == "tag_failed"
    assert "warning:" not in result.stderr
    panes = tmux_server.raw("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
    assert panes == [base]


def test_r1_3_the_placeholder_needs_no_sleep_on_the_callers_path(tmux_server: TmuxServer) -> None:
    # Review round 1, R1-3: a caller PATH with only tmux and the command; the placeholder
    # finds sleep on its own fixed PATH.
    bin_dir = tmux_server.root / "bin"
    bin_dir.mkdir()
    (bin_dir / "tmux").symlink_to(tmux_server.tmux_bin)
    tool = bin_dir / "only-tool"
    tool.write_text("#!/bin/sh\nexec /bin/sleep 60\n")
    tool.chmod(0o755)
    for session_exists in (False, True):
        out = opened(tmux_server, "--", "only-tool", environ={"PATH": str(bin_dir)})
        assert out["state"] == "running", session_exists


def test_usage_errors_start_nothing(tmux_server: TmuxServer) -> None:
    for argv in (["--", "A=b"], ["--env", "PATH=/x"], ["--env", "A=1", "--env", "A=2"]):
        result = termctl(tmux_server, "--json", "open", *argv)
        assert result.code == 2, argv
    assert not tmux_server.alive()
    assert not os.path.exists(tmux_server.socket_path)
