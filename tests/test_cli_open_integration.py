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


@needs_proc
def test_r2_1_the_session_entry_outside_update_environment_takes_precedence_over_the_global(
    tmux_server: TmuxServer,
) -> None:
    # Review round 2, R2-1 (documented fallback, interface.md "--env scope"): TZ is not in
    # update-environment, so without --env later windows would get the server's global TZ;
    # the removal marker termctl chains leaves them without TZ instead.
    tmux_server.env["TZ"] = "Server/Zone"
    tmux_server.start()
    del tmux_server.env["TZ"]
    assert "TZ=Server/Zone" in global_env(tmux_server)
    first = opened(tmux_server, "--session", "s2", "--env", "TZ=Mine/Option")
    assert environ_of(tmux_server, first["pane_id"])["TZ"] == "Mine/Option"
    assert "-TZ" in session_env(tmux_server, "s2")
    later = later_window(tmux_server, "s2")
    assert "TZ" not in environ_of(tmux_server, later)


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


# --- step 7: the process ------------------------------------------------------------------------


def proc_stat_fields(pid: int) -> list[str]:
    """Fields 3 on of ``/proc/<pid>/stat`` (index 0 is the state, 1 the parent, 19 the start)."""
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


@needs_proc
def test_e6_the_real_commands_pid_is_recorded_with_its_start_time(tmux_server: TmuxServer) -> None:
    out = opened(tmux_server, "--", "sleep", "61")
    pane = out["pane_id"]
    pid = out["pid"]
    assert out["state"] == "running"
    # The query ran after respawn-pane: the PID is the real command, never the placeholder.
    assert isinstance(pid, int) and str(pid) == fmt(tmux_server, pane, "#{pane_pid}")
    assert Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", b"61"]
    fields = proc_stat_fields(pid)
    assert fields[1] == fmt(tmux_server, pane, "#{pid}")  # its parent is the server
    recorded = fmt(tmux_server, pane, "#{@termctl_pid} #{@termctl_pid_start}")
    assert recorded == f"{pid} {fields[19]}"
    shown = termctl(tmux_server, "--json", "show", out["id"]).json()
    assert shown["pid"] == pid


@needs_proc
def test_e6_without_argv_the_login_shells_pid_is_recorded(tmux_server: TmuxServer) -> None:
    out = opened(tmux_server)
    pid = out["pid"]
    assert isinstance(pid, int) and str(pid) == fmt(tmux_server, out["pane_id"], "#{pane_pid}")
    assert proc_stat_fields(pid)[1] == fmt(tmux_server, out["pane_id"], "#{pid}")


def test_e6_a_pane_dead_at_the_recording_gets_no_pid(tmux_server: TmuxServer) -> None:
    # A command that has exited by the time termctl checks its process is never recorded: the
    # pane is dead, or its process a zombie or reaped (keep always keeps the dead pane).
    out = opened(tmux_server, "--keep-on-exit", "always", "--", "true")
    assert wait_for(lambda: fmt(tmux_server, out["pane_id"], "#{pane_dead}") == "1")
    pid, start = fmt(tmux_server, out["pane_id"], "#{@termctl_pid} #{@termctl_pid_start}").split(
        " "
    )
    if out["pid"] is None:
        assert (pid, start) == ("", "")
    else:
        # A slow host may still have seen `true` running: then both options are set.
        assert pid == str(out["pid"]) and start != ""


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
    # Logged with result error (interface.md "Audit and log"), under the private HOME.
    log = tmux_server.root / ".local" / "state" / "termctl" / "log.jsonl"
    warning, record = [json.loads(line) for line in log.read_text().splitlines()]
    validate(warning, "termctl/log.json")
    validate(record, "termctl/log.json")
    # The cleanup warning is logged too, before the open record.
    assert (warning["event"], warning["result"]) == ("warning", "warning")
    assert "unmanaged" in warning["message"]
    assert (record["event"], record["result"], record["exit_code"]) == ("open", "error", 12)
    assert record["socket_path"] == tmux_server.socket_path


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


# --- hooks (interface.md "Hooks"): real hook scripts ------------------------------------------


def hook_script(tmp_path: Path, name: str, body: str) -> str:
    """A private executable hook script; its path is the hook's cmd."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return str(path)


def test_a_vetoing_pre_open_hook_starts_no_server(tmux_server: TmuxServer, tmp_path: Path) -> None:
    seen = tmp_path / "payload.json"
    gate = hook_script(tmp_path, "gate", f"cat > '{seen}'; echo 'not today' >&2; exit 1")
    result = termctl(tmux_server, "--json", "open", "--hook", f"pre-open={gate}", "--", "true")
    assert result.code == 11, result.stderr
    err = result.json()["error"]
    assert (err["code"], err["hook"], err["hook_stderr"]) == ("vetoed", gate, "not today\n")
    validate(json.loads(seen.read_text(encoding="utf-8")), "termctl/hooks/pre-open.json")
    assert not tmux_server.alive()
    assert not Path(tmux_server.socket_path).exists()


def test_an_escalation_creates_nothing_and_an_override_run_opens(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    tmux_server.start()
    gate = hook_script(tmp_path, "gate", 'printf \'{"decision_id": "gate-1"}\'; exit 13')
    before = tmux_server.raw("list-windows", "-a").stdout
    escalated = termctl(tmux_server, "--json", "open", "--hook", f"pre-open={gate}", "--", "true")
    assert escalated.code == 13, escalated.stderr
    assert escalated.json()["error"]["decision_ids"] == ["gate-1"]
    assert tmux_server.raw("list-windows", "-a").stdout == before
    out = opened(
        tmux_server, "--actor", "human:tester", "--override-escalation", "--hook",
        f"pre-open={gate}", "--keep-on-exit", "always", "--", "true",
    )  # fmt: skip
    assert out["decision_ids"] == ["gate-1"]
    assert pane_exists(tmux_server, out["pane_id"])


def test_a_post_open_hook_gets_the_open_output(tmux_server: TmuxServer, tmp_path: Path) -> None:
    seen = tmp_path / "payload.json"
    notify = hook_script(tmp_path, "notify", f"cat > '{seen}'; echo \"$TERMCTL_ID\" >> '{seen}.id'")
    out = opened(tmux_server, "--hook", f"post-open={notify}", "--keep-on-exit", "always", "--",
                 "true")  # fmt: skip
    data = json.loads(seen.read_text(encoding="utf-8"))
    validate(data, "termctl/hooks/post-open.json")
    assert data["pane"] == out
    assert Path(f"{seen}.id").read_text(encoding="utf-8") == f"{out['id']}\n"


# --- --dry-run (#18) ----------------------------------------------------------------------------


def dry_run(server: TmuxServer, *argv: str) -> Any:
    result = termctl(server, "--json", "open", "--dry-run", *argv)
    assert result.code == 0, result.stdout + result.stderr
    out = result.json()
    validate(out, "termctl/cli/open-dry-run.json")
    return out


def test_dry_run_changes_nothing_on_a_running_server(tmux_server: TmuxServer) -> None:
    tmux_server.start(SESSION)
    before = tmux_server.raw("list-panes", "-a", "-F", "#{pane_id} #{window_id}").stdout
    out = dry_run(tmux_server, "--env", "SECRET=s3cret-value", "--", "true")
    assert out["session_exists"] is True
    assert out["tmux_commands"][0][3] == "new-window"
    assert "s3cret" not in json.dumps(out)
    after = tmux_server.raw("list-panes", "-a", "-F", "#{pane_id} #{window_id}").stdout
    assert after == before
    assert dry_run(tmux_server, "--session", "other")["session_exists"] is False
    assert tmux_server.raw("has-session", "-t", "=other:").returncode != 0


def test_dry_run_starts_no_server(tmux_server: TmuxServer) -> None:
    out = dry_run(tmux_server, "--", "true")
    assert out["session_exists"] is None
    assert out["tmux_commands"][0][3:5] == ["set", "-s"]
    assert not os.path.exists(tmux_server.socket_path)


def test_dry_run_creates_no_default_directory(tmux_server: TmuxServer) -> None:
    # A socket in default directory form whose directory does not exist yet: a real run would
    # create the directory (Open order step 5), a dry run must not.
    tmpdir = tmux_server.root / "t"
    tmpdir.mkdir()
    tmpdir.chmod(0o700)  # whatever the umask: the socket checks refuse a group-writable one
    default_dir = tmpdir / f"tmux-{os.getuid()}"
    socket_path = str(default_dir / "default")
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        ["--socket", socket_path, "--json", "open", "--dry-run", "--", "true"],
        environ={**tmux_server.env, "XDG_CONFIG_HOME": NO_CONFIG, "TMUX_TMPDIR": str(tmpdir)},
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0, stdout.getvalue() + stderr.getvalue()
    out = json.loads(stdout.getvalue())
    validate(out, "termctl/cli/open-dry-run.json")
    assert out["socket_path"] == socket_path
    assert out["session_exists"] is None
    assert not default_dir.exists()
