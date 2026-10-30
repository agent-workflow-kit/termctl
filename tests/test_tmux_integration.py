"""Real-tmux regression tests for the argument builder and guarded writes: spikes E1, E3, E4,
C9 and F5 (design.md "Live verification"; interface.md "tmux argument rules").

Each test runs its own private server (``tmux_server`` in conftest.py). Values go through the
builder; the server is inspected with ``raw`` tmux calls, the oracle.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from termctl import tmux_backend as tb
from termctl.tmux_backend import Command, Format, Invocation, command

from .conftest import TmuxServer, wait_for

pytestmark = pytest.mark.tmux

ID = "tc-abcdefgh"
OTHER_ID = "tc-zzzzzzzz"

# Literal values: option values, -e values, argv. No NUL (argv cannot hold it).
LITERALS = [
    "",
    ";",
    ";;",
    "a;",
    "a\\;",
    "a\\",
    "\\",
    "-",
    "--",
    "-x",
    "--json",
    "#",
    "##",
    "#{pane_id}",
    "#(echo pwned)",
    "#[fg=red]",
    "'",
    "a'b",
    '"',
    "$HOME",
    "~",
    "{}",
    "}",
    "a,b",
    "a b",
    " lead",
    "a\tb",
    "a\nb",
    "\x1b[31m",
    "é中\U0001f514",
    "x" * 4000,
]

# Option values as tmux prints them back: it escapes ESC and other C0 characters on output
# (tab and newline are kept), so those are left to the argv and -e tests, which read them raw.
OPTION_VALUES = [v for v in LITERALS if "\x1b" not in v]

# Values for format-expanded positions: valid names (no control characters, no "#[").
NAMES = [
    "#",
    "##",
    "###",
    "a#b",
    "#{pane_id}",
    "#{?pane_dead,a,b}",
    "PR#42 review",
    "#;",
    ";",
    "a;",
    "a\\;",
    "-x",
    "--",
    "'",
    "a'b",
    '"',
    "$HOME",
    "~",
    "{",
    "}",
    "a,b",
    "é中\U0001f514",
    "🔔 KEY-12",
]


def _run(server: TmuxServer, *commands: Command) -> tuple[int, str, str]:
    result = server.tmux().run(Invocation.of(*commands))
    return result.returncode, result.stdout, result.stderr


def _show_pane_option(server: TmuxServer, pane_id: str, name: str) -> str:
    result = server.raw("show-options", "-p", "-q", "-v", "-t", pane_id, name)
    assert result.returncode == 0, result.stderr
    # An unset option prints nothing; a set one its value and a newline.
    return result.stdout.removesuffix("\n")


def _display(server: TmuxServer, target: str, fmt: str) -> str:
    result = server.raw("display-message", "-p", "-t", target, fmt)
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def _set_pane(pane_id: str, name: str, value: str) -> Command:
    return command("set-option").flag("-p").target(tb.pane(pane_id)).arg(name).arg(value).build()


def _tag(server: TmuxServer, pane_id: str, termctl_id: str = ID) -> None:
    window_id = _display(server, pane_id, "#{window_id}")
    rc, _, err = _run(
        server,
        _set_pane(pane_id, "@termctl_pane", pane_id),
        _set_pane(pane_id, "@termctl_id", termctl_id),
        _set_pane(pane_id, "@termctl_window", window_id),
    )
    assert rc == 0, err


def _new_window(server: TmuxServer, builder_tail: Command | None = None) -> tuple[str, str]:
    cmd = builder_tail or (
        command("new-window")
        .flag("-d")
        .flag("-P")
        .format("-F", Format("#{pane_id} #{window_id}"))
        .target(tb.session("base"))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("600")
        .build()
    )
    rc, out, err = _run(server, cmd)
    assert rc == 0, err
    pane_id, window_id = out.split()
    return pane_id, window_id


def _as_window_name(name: str) -> str:
    """How tmux reports a name set by ``rename-window``: it stores that name vis-escaped, so
    a backslash reads back doubled (``new-window -n`` keeps it as given)."""
    return name.replace("\\", "\\\\")


def _no_canary(canary: Path) -> bool:
    # #() jobs run asynchronously; give one a moment to show up before concluding it did not.
    time.sleep(0.3)
    return not canary.exists()


# --- E1: a trailing ";" never splits the invocation ---------------------------------------------


def test_e1_control_an_unescaped_trailing_semicolon_injects_a_command(
    tmux_server: TmuxServer,
) -> None:
    # Proves the hazard exists in this tmux, so the next test can fail.
    tmux_server.start()
    tmux_server.raw("new-window", "-d", "-t", "=base:", "--", "/usr/bin/env", "--", "true;",
                    "set-option", "-g", "@pwned", "1")  # fmt: skip
    assert tmux_server.raw("show-options", "-g", "-v", "@pwned").stdout == "1\n"


def test_e1_trailing_semicolon_in_argv_runs_no_injected_command(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    cmd = (
        command("new-window")
        .flag("-d")
        .target(tb.session("base"))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("true;")
        .arg("set-option")
        .arg("-g")
        .arg("@pwned")
        .arg("1")
        .build()
    )
    _run(tmux_server, cmd)
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@pwned").stdout == ""


def test_e1_trailing_semicolon_in_options_runs_no_injected_command(
    tmux_server: TmuxServer,
) -> None:
    pane_id = tmux_server.start()
    # Each value ends in ";" and is followed by elements that would form a valid command.
    cmd = command("set-option").flag("-p").target(tb.pane(pane_id)).arg("@a").arg("x;").build()
    rc, _, err = _run(
        tmux_server, cmd, command("set-option").flag("-g").arg("@b").arg("1;").build()
    )
    assert rc == 0, err
    assert _show_pane_option(tmux_server, pane_id, "@a") == "x;"
    assert tmux_server.raw("show-options", "-g", "-v", "@b").stdout == "1;\n"


@pytest.mark.parametrize("value", OPTION_VALUES)
def test_pane_option_values_round_trip(tmux_server: TmuxServer, value: str) -> None:
    pane_id = tmux_server.start()
    rc, _, err = _run(tmux_server, _set_pane(pane_id, "@termctl_name", value))
    assert rc == 0, err
    assert _show_pane_option(tmux_server, pane_id, "@termctl_name") == value


@pytest.mark.parametrize("value", [v for v in LITERALS if v])
def test_argv_and_env_values_round_trip(tmux_server: TmuxServer, value: str) -> None:
    tmux_server.start()
    out = tmux_server.root / "out"
    script = 'printf "%s\\0" "$V" "$@" > "$0.tmp" && mv "$0.tmp" "$0"'
    cmd = (
        command("new-window")
        .flag("-d")
        .target(tb.session("base"))
        .env("V", value)
        .arg("/usr/bin/env")
        .arg("--")
        .arg("/bin/sh")
        .arg("-c")
        .arg(script)
        .arg(str(out))
        .arg(value)
        .arg(value + ";")
        .build()
    )
    rc, _, err = _run(tmux_server, cmd)
    assert rc == 0, err
    assert wait_for(out.exists)
    assert out.read_bytes().decode().split("\0")[:-1] == [value, value, value + ";"]


# --- E3: "--" before the first positional -------------------------------------------------------


def test_e3_leading_dash_positionals_are_not_flags(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    rc, _, err = _run(
        tmux_server,
        command("set-option").flag("-p").target(tb.pane(pane_id)).arg("@a").arg("-g").build(),
        command("rename-window")
        .target(tb.window(_display(tmux_server, pane_id, "#{window_id}")))
        .name("-t")
        .build(),
    )
    assert rc == 0, err
    assert _show_pane_option(tmux_server, pane_id, "@a") == "-g"
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@a").stdout == ""
    assert _display(tmux_server, pane_id, "#{window_name}") == "-t"


# --- E4: "#" doubling; "#(" never executes ------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_e4_names_round_trip_in_new_window_and_rename_window(
    tmux_server: TmuxServer, name: str
) -> None:
    tmux_server.start()
    cmd = (
        command("new-window")
        .flag("-d")
        .flag("-P")
        .format("-F", Format("#{pane_id} #{window_id}"))
        .target(tb.session("base"))
        .option("-n", name)
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("600")
        .build()
    )
    pane_id, window_id = _new_window(tmux_server, cmd)
    # new-window -n keeps a backslash as given; rename-window doubles it (_as_window_name).
    assert _display(tmux_server, pane_id, "#{window_name}") == name
    rc, _, err = _run(
        tmux_server, command("rename-window").target(tb.window(window_id)).name(name + "2").build()
    )
    assert rc == 0, err
    assert _display(tmux_server, pane_id, "#{window_name}") == _as_window_name(name + "2")


@pytest.mark.parametrize("dirname", ["#", "a#b", "#{pane_id}", "a;", ";", "-x", "'", "$HOME", "é"])
def test_e4_cwd_round_trips(tmux_server: TmuxServer, dirname: str) -> None:
    tmux_server.start()
    cwd = tmux_server.root / dirname
    cwd.mkdir()
    cmd = (
        command("new-window")
        .flag("-d")
        .flag("-P")
        .format("-F", Format("#{pane_id} #{window_id}"))
        .target(tb.session("base"))
        .option("-c", str(cwd))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("600")
        .build()
    )
    pane_id, _ = _new_window(tmux_server, cmd)
    assert wait_for(lambda: _display(tmux_server, pane_id, "#{pane_current_path}") == str(cwd))


def test_e4_control_an_undoubled_command_format_runs(tmux_server: TmuxServer) -> None:
    # Proves "#(" executes in a rename-window name on this tmux, so the next tests can fail.
    pane_id = tmux_server.start()
    canary = tmux_server.root / "control"
    tmux_server.raw("rename-window", "-t", pane_id, "--", f"#(touch {canary})")
    assert wait_for(canary.exists)


def test_e4_command_formats_never_run(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    canary = tmux_server.root / "canary"
    # No "/", so the payload can also be a directory name; jobs run with HOME set to root.
    payload = "#(cd; touch canary)"
    cwd = tmux_server.root / payload
    cwd.mkdir()
    new_window = (
        command("new-window")
        .flag("-d")
        .target(tb.session("base"))
        .option("-n", payload)
        .option("-c", str(cwd))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("600")
        .build()
    )
    new_session = (
        command("new-session")
        .flag("-d")
        .option("-s", payload)
        .option("-n", payload)
        .option("-c", str(cwd))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("600")
        .build()
    )
    rename = command("rename-window").target(tb.window(window_id)).name(payload).build()
    _tag(tmux_server, pane_id)
    guarded_rename = tb.guarded(
        tb.pane(pane_id),
        tb.owned_single_pane_window(ID),
        command("rename-window").target(tb.window(window_id)).name(payload + "g").build(),
    )
    for cmd in (new_window, new_session, rename, guarded_rename):
        rc, out, err = _run(tmux_server, cmd)
        assert rc == 0, err
        assert tb.guard_matched(out)
    assert _no_canary(canary)
    assert _display(tmux_server, pane_id, "#{window_name}") == payload + "g"
    sessions = tmux_server.raw("list-sessions", "-F", "#{session_name}").stdout.splitlines()
    assert payload in sessions


# --- C9: doubling does not neutralize "#[" ------------------------------------------------------


@pytest.mark.parametrize("value", ["#[fg=red]x", "a##[b"])
def test_c9_status_styles_never_reach_a_format_expanded_position(value: str) -> None:
    for flag in ("-n", "-c", "-s"):
        with pytest.raises(ValueError):
            command("new-window").option(flag, value)
    with pytest.raises(ValueError):
        command("rename-window").name(value)


# --- F5: guarded writes -------------------------------------------------------------------------


@pytest.mark.parametrize("value", OPTION_VALUES)
def test_f5_guarded_option_values_round_trip(tmux_server: TmuxServer, value: str) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_name", value))
    rc, out, err = _run(tmux_server, cmd)
    assert rc == 0, err
    assert tb.guard_matched(out)
    assert _show_pane_option(tmux_server, pane_id, "@termctl_name") == value
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@termctl_name").stdout == ""


@pytest.mark.parametrize("name", NAMES)
def test_f5_guarded_rename_round_trips(tmux_server: TmuxServer, name: str) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    _tag(tmux_server, pane_id)
    rename = command("rename-window").target(tb.window(window_id)).name(name).build()
    rc, out, err = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    )
    assert (rc, tb.guard_matched(out)) == (0, True), err
    assert _display(tmux_server, pane_id, "#{window_name}") == _as_window_name(name)


def test_f5_guarded_env_value_round_trips(tmux_server: TmuxServer) -> None:
    pane_id, _ = _new_window_in_started(tmux_server)
    _tag(tmux_server, pane_id)
    out = tmux_server.root / "out"
    value = "it's; #(x) $HOME ~ \\ \n;"
    respawn = (
        command("respawn-pane")
        .flag("-k")
        .target(tb.pane(pane_id))
        .env("V", value)
        .arg("/usr/bin/env")
        .arg("--")
        .arg("/bin/sh")
        .arg("-c")
        .arg('printf "%s" "$V" > "$0.tmp" && mv "$0.tmp" "$0"; exec sleep 600')
        .arg(str(out))
        .build()
    )
    rc, stdout, err = _run(tmux_server, tb.guarded(tb.pane(pane_id), tb.owned(ID), respawn))
    assert (rc, tb.guard_matched(stdout)) == (0, True), err
    assert wait_for(out.exists)
    assert out.read_text() == value


def _new_window_in_started(server: TmuxServer) -> tuple[str, str]:
    server.start()
    return _new_window(server)


def test_f5_guard_chains_inner_commands_and_reports_their_output(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    display = (
        command("display-message")
        .flag("-p")
        .target(tb.pane(pane_id))
        .format_arg(Format("#{pane_id} #{@termctl_name}"))
        .build()
    )
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_name", "n;"), display
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, out) == (0, f"{pane_id} n;\n"), err


def test_f5_wrong_id_is_a_nomatch_and_changes_nothing(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(OTHER_ID), command("kill-pane").target(tb.pane(pane_id)).build()
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, out) == (0, "termctl-nomatch\n"), err
    assert not tb.guard_matched(out)
    assert tmux_server.alive()


def test_f5_untagged_pane_is_a_nomatch(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@x", "1"))
    rc, out, _ = _run(tmux_server, cmd)
    assert (rc, tb.guard_matched(out)) == (0, False)
    assert _show_pane_option(tmux_server, pane_id, "@x") == ""


def test_f5_inherited_window_option_is_a_nomatch(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    split = ["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane_id]
    other_pane = tmux_server.raw(*split, "--", "/usr/bin/env", "--", "sleep", "600").stdout.strip()
    # The other pane inherits window-level values, which never equal its own pane id.
    tmux_server.raw("set-option", "-w", "-t", pane_id, "@termctl_pane", pane_id)
    tmux_server.raw("set-option", "-w", "-t", pane_id, "@termctl_id", ID)
    cmd = tb.guarded(
        tb.pane(other_pane), tb.owned(ID), command("kill-pane").target(tb.pane(other_pane)).build()
    )
    rc, out, _ = _run(tmux_server, cmd)
    assert (rc, tb.guard_matched(out)) == (0, False)
    assert tmux_server.raw("list-panes", "-t", other_pane).returncode == 0


def test_f5_missing_pane_is_a_nomatch(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    cmd = tb.guarded(
        tb.pane("%999"), tb.owned(ID), command("kill-pane").target(tb.pane("%999")).build()
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, tb.guard_matched(out)) == (0, False), err


def test_f5_restarted_server_reusing_the_pane_id_is_a_nomatch(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    tmux_server.raw("kill-server")
    assert wait_for(lambda: not tmux_server.alive())
    new_pane = tmux_server.start("other")
    assert new_pane == pane_id  # tmux restarts numbering
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(ID), command("kill-pane").target(tb.pane(pane_id)).build()
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, tb.guard_matched(out)) == (0, False), err
    assert tmux_server.raw("list-panes", "-t", new_pane).returncode == 0


def test_f5_rename_guard_refuses_a_split_window(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    _tag(tmux_server, pane_id)
    tmux_server.raw("rename-window", "-t", window_id, "before")
    tmux_server.raw("split-window", "-d", "-t", pane_id, "--", "/usr/bin/env", "--", "sleep", "600")
    rename = command("rename-window").target(tb.window(window_id)).name("after").build()
    rc, out, _ = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    )
    assert (rc, tb.guard_matched(out)) == (0, False)
    assert _display(tmux_server, pane_id, "#{window_name}") == "before"


def test_f5_rename_guard_refuses_a_moved_pane(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    # The recorded window is no longer the pane's window.
    tmux_server.raw("set-option", "-p", "-t", pane_id, "@termctl_window", "@999")
    rename = (
        command("rename-window")
        .target(tb.window(_display(tmux_server, pane_id, "#{window_id}")))
        .name("x")
        .build()
    )
    rc, out, _ = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    )
    assert (rc, tb.guard_matched(out)) == (0, False)


def test_f5_failing_inner_command_exits_1(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(ID), command("kill-pane").target(tb.pane("%999")).build()
    )
    rc, out, err = _run(tmux_server, cmd)
    assert rc == 1 and tb.guard_matched(out) and err


def test_untagged_and_cleanup_guards(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    mark = _set_pane(pane_id, "@seen", "1")
    rc, out, _ = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.untagged_in_window("@999"), mark)
    )
    assert (rc, tb.guard_matched(out)) == (0, False)
    rc, out, _ = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.untagged_in_window(window_id), mark)
    )
    assert (rc, tb.guard_matched(out)) == (0, True)
    _tag(tmux_server, pane_id)
    rc, out, _ = _run(
        tmux_server, tb.guarded(tb.pane(pane_id), tb.untagged_in_window(window_id), mark)
    )
    assert (rc, tb.guard_matched(out)) == (0, False)
    rc, out, _ = _run(tmux_server, tb.guarded(tb.pane(pane_id), tb.cleanup(window_id), mark))
    assert (rc, tb.guard_matched(out)) == (0, True)
    # Tagged by someone else (another pane id): the cleanup kill refuses.
    tmux_server.raw("set-option", "-p", "-t", pane_id, "@termctl_pane", "%999")
    rc, out, _ = _run(tmux_server, tb.guarded(tb.pane(pane_id), tb.cleanup(window_id), mark))
    assert (rc, tb.guard_matched(out)) == (0, False)


# --- chains and exact targets -------------------------------------------------------------------


def test_chain_stops_at_the_first_failure(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    rc, _, err = _run(
        tmux_server,
        command("set-option").flag("-g").arg("@a").arg("1").build(),
        command("kill-pane").target(tb.pane("%999")).build(),
        command("set-option").flag("-g").arg("@b").arg("1").build(),
    )
    assert rc == 1 and err
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@a").stdout == "1\n"
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@b").stdout == ""


def test_session_targets_are_exact(tmux_server: TmuxServer) -> None:
    tmux_server.start("work")
    cmd = (
        command("new-window")
        .flag("-d")
        .target(tb.session("wor"))
        .arg("/usr/bin/env")
        .arg("--")
        .arg("true")
        .build()
    )
    rc, _, _ = _run(tmux_server, cmd)
    assert rc == 1


def test_verbose_trace_hides_env_values(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    trace: list[str] = []
    cmd = (
        command("new-window")
        .flag("-d")
        .target(tb.session("base"))
        .env("TOKEN", "hunter2")
        .arg("/usr/bin/env")
        .arg("--")
        .arg("true")
        .build()
    )
    assert tmux_server.tmux(trace).run(Invocation.of(cmd)).returncode == 0
    assert len(trace) == 1 and "TOKEN" in trace[0] and "hunter2" not in trace[0]
