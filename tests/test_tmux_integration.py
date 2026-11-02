"""Real-tmux regression tests for the argument builder and guarded writes: spikes E1, E3, E4,
C9 and F5 (design.md "Live verification"; interface.md "tmux argument rules").

Each test runs its own private server (``tmux_server`` in conftest.py). Values go through the
builder; the server is inspected with ``raw`` tmux calls, the oracle.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
from pathlib import Path

import pytest

from termctl import tmux_backend as tb
from termctl.tmux_backend import Command, GuardOutcome, Invocation, command

from .conftest import TmuxServer, tmux_stored_name, wait_for

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
    # tmux 3.2 stores a "$" escaped only before an ASCII letter, "_" or "{" (tmux_stored_name).
    "a$b c$",
    "${x}",
    "$1",
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
    # Newlines next to whitespace, "#" and backslashes, which tmux's parser alters inside a
    # quoted guard command unless quote() writes them outside the quotes (review round 1).
    "a\n  b",
    "if 1:\n    print(1)",
    "a\\\nb",
    "a\\\\\\\nb",
    "\\\n",
    "a\\\n'b",
    "x\n set -g @pwned 1",
    "a\r\nb",
    "a\n#x",
    "a\n#{pane_id}",
    "a\n\tb",
    "\n\n",
    "\n",
    "a\n}",
    "#(echo pwned)\n#(echo pwned)",
]

# Option values as tmux prints them back: tmux 3.3+ escapes ESC, CR and other C0 characters on
# output (tab and newline are kept) and 3.2 prints them raw, so those are left to the argv and -e
# tests, which read them raw.
OPTION_VALUES = [v for v in LITERALS if "\x1b" not in v and "\r" not in v]

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
    # tmux 3.2 stores a "$" escaped only before an ASCII letter, "_" or "{" (tmux_stored_name).
    "a$b c$",
    "${x}",
    "$1",
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


def _guard(server: TmuxServer, cmd: Command) -> GuardOutcome:
    return tb.guard_outcome(server.tmux().run(Invocation.of(cmd)))


def _show_pane_option(server: TmuxServer, pane_id: str, name: str) -> str:
    result = server.raw_bytes("show-options", "-p", "-q", "-v", "-t", pane_id, name)
    assert result.returncode == 0, result.stderr
    # An unset option prints nothing; a set one its value and a newline.
    return result.stdout.decode().removesuffix("\n")


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
        .format("-F", tb.fields("pane_id", "window_id"))
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


# How long _run_holding_client keeps the client open for a "#(" job: the control test proves
# an undoubled job fires well within it on every tmux tested (3.2a, 3.5a, 3.6a).
JOB_WINDOW = 1.0


def _run_holding_client(
    server: TmuxServer, args: tuple[str, ...] | list[str], canary: Path
) -> subprocess.CompletedProcess[str]:
    """Run tmux ``args`` (no binary, no ``-S``), then, in the same invocation, a ``run-shell``
    that waits until ``canary`` exists or ``JOB_WINDOW`` seconds pass.

    A "#(" job started while a command expands a format belongs to the client that sent the
    command, and tmux kills a client's jobs when it exits (format_lost_client). A lone command
    races its own client: tmux 3.2a and 3.6a lose that race every time, 3.5a wins it. Holding
    the client gives every job the same chance to run, so a test that a "#(" never runs cannot
    pass only because its client exited first. The hold has no "#": run-shell expands formats.
    """
    path = shlex.quote(str(canary))
    polls = round(JOB_WINDOW / 0.02)
    hold = f"i=0; while [ ! -e {path} ] && [ $i -lt {polls} ]; do sleep 0.02; i=$((i + 1)); done"
    return server.raw(*args, tb.SEPARATOR, "run-shell", hold)


def _builder_args(server: TmuxServer, *commands: Command) -> list[str]:
    """The builder's argv for ``commands``, without the binary and ``-S <socket>``."""
    return Invocation.of(*commands).argv(server.tmux_bin, server.socket_path)[3:]


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
        .format("-F", tb.fields("pane_id", "window_id"))
        .target(tb.session("base"))
        .window_name(name)
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
    assert _display(tmux_server, pane_id, "#{window_name}") == tmux_stored_name(name + "2")


@pytest.mark.parametrize("dirname", ["#", "a#b", "#{pane_id}", "a;", ";", "-x", "'", "$HOME", "é"])
def test_e4_cwd_round_trips(tmux_server: TmuxServer, dirname: str) -> None:
    tmux_server.start()
    cwd = tmux_server.root / dirname
    cwd.mkdir()
    cmd = (
        command("new-window")
        .flag("-d")
        .flag("-P")
        .format("-F", tb.fields("pane_id", "window_id"))
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
    # Proves "#(" executes in a rename-window name on this tmux, within the JOB_WINDOW the
    # next test gives its payloads, so that test can fail (_run_holding_client).
    pane_id = tmux_server.start()
    canary = tmux_server.root / "control"
    rename = ("rename-window", "-t", pane_id, "--", f"#(touch {canary})")
    result = _run_holding_client(tmux_server, rename, canary)
    assert result.returncode == 0, result.stderr
    assert canary.exists()


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
        .window_name(payload)
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
        .session_name("-s2")
        .window_name(payload)
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
    # One invocation, so one client held open for JOB_WINDOW covers every payload's job.
    args = _builder_args(tmux_server, new_window, new_session, rename, guarded_rename)
    result = _run_holding_client(tmux_server, args, canary)
    assert tb.guard_outcome(result) is GuardOutcome.MATCHED, result.stderr
    assert not canary.exists()
    assert _display(tmux_server, pane_id, "#{window_name}") == payload + "g"
    windows = tmux_server.raw("list-windows", "-a", "-F", "#{session_name}|#{window_name}")
    assert f"-s2|{payload}" in windows.stdout.splitlines()


# --- C9: doubling does not neutralize "#[" ------------------------------------------------------


def _attached_status_line(server: TmuxServer) -> str:
    """The status line of a client attached to ``server``'s ``base`` session, with its colours
    as SGR sequences: the client runs in a pane of a second private server, which captures it."""
    outer = TmuxServer(server.tmux_bin, server.root, str(server.root / "outer"), server.env)
    attach = [server.tmux_bin, "-S", server.socket_path, "attach", "-t", "=base:"]
    try:
        result = outer.raw(
            "-f", "/dev/null", "new-session", "-d", "-s", "outer", "-x", "80", "-y", "24",
            "--", "/usr/bin/env", "--", *attach,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr
        lines: list[str] = []

        def rendered() -> bool:
            capture = outer.raw("capture-pane", "-e", "-p", "-t", "=outer:")
            lines[:] = capture.stdout.rstrip("\n").splitlines()[-1:]
            return bool(lines) and "[base]" in lines[0]

        assert wait_for(rendered)
        return lines[0]
    finally:
        outer.raw("kill-server")


def test_c9_control_a_status_style_in_a_window_name_applies(tmux_server: TmuxServer) -> None:
    # Proves the hazard on this tmux (spike C9): "#[" in a window name is a style on the status
    # line, and doubling does not make it round-trip, so termctl refuses "#[" instead.
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    tmux_server.raw("set-option", "-g", "status-right", "")
    tmux_server.raw("rename-window", "-t", window_id, "--", "#[fg=red]x")
    status = _attached_status_line(tmux_server)
    # x is drawn red (SGR 31), and the style text is not shown.
    assert "\x1b[31mx" in status and "fg=red" not in status
    # Doubled, "##[fg=red]x" reads back as given rather than as the name "#[fg=red]x".
    tmux_server.raw("rename-window", "-t", window_id, "--", "##[fg=red]x")
    assert _display(tmux_server, pane_id, "#{window_name}") == "##[fg=red]x"


@pytest.mark.parametrize("value", ["#[fg=red]x", "a##[b"])
def test_c9_status_styles_never_reach_a_format_expanded_position(value: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").window_name(value)
    with pytest.raises(ValueError):
        command("new-window").option("-c", "/" + value)
    with pytest.raises(ValueError):
        command("new-session").session_name(value)
    with pytest.raises(ValueError):
        command("rename-window").name(value)


# --- F5: guarded writes -------------------------------------------------------------------------


@pytest.mark.parametrize("value", OPTION_VALUES)
def test_f5_guarded_option_values_round_trip(tmux_server: TmuxServer, value: str) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_name", value))
    assert _guard(tmux_server, cmd) is GuardOutcome.MATCHED
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@pwned").stdout == ""
    assert _show_pane_option(tmux_server, pane_id, "@termctl_name") == value
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@termctl_name").stdout == ""


@pytest.mark.parametrize("name", NAMES)
def test_f5_guarded_rename_round_trips(tmux_server: TmuxServer, name: str) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    _tag(tmux_server, pane_id)
    rename = command("rename-window").target(tb.window(window_id)).name(name).build()
    guarded = tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    assert _guard(tmux_server, guarded) is GuardOutcome.MATCHED
    assert _display(tmux_server, pane_id, "#{window_name}") == tmux_stored_name(name)


@pytest.mark.parametrize("value", LITERALS)
def test_f5_guarded_argv_and_env_values_round_trip(tmux_server: TmuxServer, value: str) -> None:
    pane_id, _ = _new_window_in_started(tmux_server)
    _tag(tmux_server, pane_id)
    out = tmux_server.root / "out"
    script = 'printf "%s\\0" "$V" "$@" > "$0.tmp" && mv "$0.tmp" "$0"; exec sleep 600'
    respawn = (
        command("respawn-pane")
        .flag("-k")
        .target(tb.pane(pane_id))
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
    guarded = tb.guarded(tb.pane(pane_id), tb.owned(ID), respawn)
    assert _guard(tmux_server, guarded) is GuardOutcome.MATCHED
    assert wait_for(out.exists)
    assert out.read_bytes().decode().split("\0")[:-1] == [value, value, value + ";"]
    assert tmux_server.raw("show-options", "-g", "-q", "-v", "@pwned").stdout == ""


# Every byte through a guard (review round 2, R2-1). Byte 0xff ends tmux's parse even inside
# quotes, so quote() writes it as "\377" outside them; every other byte stays quoted. Each byte
# is tried in the middle, alone, at the end and before an injection attempt, plus invalid UTF-8
# built from valid lead bytes (an overlong "\xc0\xbb" is ";" to a lax decoder). Values reach
# the builder as os.fsdecode strings, the way undecodable caller bytes arrive.
BYTE_CHUNKS = [range(low, min(low + 32, 256)) for low in range(1, 256, 32)]
INVALID_UTF8 = [
    b"\xc3;", b"\xc3", b"\xe2\x80", b"\xe2\x80;", b"\xc0\xbb", b"\xf0\x9f\x94", b"\xed\xa0\x80",
    b"\xc3\xff;", b"\xff\xff", b"\xff\n\xff", b"\xff'\xff",
]  # fmt: skip
SEQUENCES = [[bytes([b]) for b in chunk] for chunk in BYTE_CHUNKS] + [INVALID_UTF8]
SEQUENCE_IDS = [f"0x{c[0]:02x}-0x{c[-1]:02x}" for c in BYTE_CHUNKS] + ["invalid-utf8"]


def _forms(sequence: bytes) -> list[bytes]:
    injection = b"a" + sequence + b"; set -g @pwned 1"
    return [b"a" + sequence + b"b", sequence, b"a" + sequence, injection]


def _pane_option_bytes(server: TmuxServer, pane_id: str, name: str) -> bytes:
    result = server.raw_bytes("show-options", "-p", "-q", "-v", "-t", pane_id, name)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("sequences", SEQUENCES, ids=SEQUENCE_IDS)
def test_f5_every_byte_round_trips_in_a_guarded_option_value(
    tmux_server: TmuxServer, sequences: list[bytes]
) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    for form in (f for s in sequences for f in _forms(s)):
        value = os.fsdecode(form)
        cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_name", value))
        assert _guard(tmux_server, cmd) is GuardOutcome.MATCHED, form
        # The oracle: the same value set unguarded as a plain argument, which no parser sees
        # (tmux escapes some bytes when printing an option, the same way for both).
        oracle = tmux_server.raw_bytes(
            "set-option", "-p", "-t", pane_id, "@w", tb.escape_separator(value)
        )
        assert oracle.returncode == 0, oracle.stderr
        expected = _pane_option_bytes(tmux_server, pane_id, "@w")
        assert expected, form
        assert _pane_option_bytes(tmux_server, pane_id, "@termctl_name") == expected, form
        assert tmux_server.raw("show-options", "-g", "-q", "-v", "@pwned").stdout == "", form


@pytest.mark.parametrize("sequences", SEQUENCES, ids=SEQUENCE_IDS)
def test_f5_every_byte_round_trips_in_guarded_argv_and_env(
    tmux_server: TmuxServer, sequences: list[bytes]
) -> None:
    pane_id, _ = _new_window_in_started(tmux_server)
    _tag(tmux_server, pane_id)
    out = tmux_server.root / "out"
    script = 'printf "%s\\0" "$V" "$@" > "$0.tmp" && mv "$0.tmp" "$0"; exec sleep 600'
    for sequence in sequences:
        forms = _forms(sequence)
        out.unlink(missing_ok=True)
        respawn = (
            command("respawn-pane")
            .flag("-k")
            .target(tb.pane(pane_id))
            .env("V", os.fsdecode(forms[-1]))
            .arg("/usr/bin/env")
            .arg("--")
            .arg("/bin/sh")
            .arg("-c")
            .arg(script)
            .arg(str(out))
        )
        for form in forms:
            respawn.arg(os.fsdecode(form))
        guarded = tb.guarded(tb.pane(pane_id), tb.owned(ID), respawn.build())
        assert _guard(tmux_server, guarded) is GuardOutcome.MATCHED, sequence
        assert wait_for(out.exists), sequence
        assert out.read_bytes().split(b"\0")[:-1] == [forms[-1], *forms], sequence
        assert tmux_server.raw("show-options", "-g", "-q", "-v", "@pwned").stdout == "", sequence


def _new_window_in_started(server: TmuxServer) -> tuple[str, str]:
    server.start()
    return _new_window(server)


def test_f5_guard_chains_inner_commands_and_reports_their_output(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    # Only ids and numbers are printed inside a guard (review round 4, R4-4).
    display = (
        command("display-message")
        .flag("-p")
        .target(tb.pane(pane_id))
        .format_arg(tb.fields("pane_id", "window_id"))
        .build()
    )
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_name", "n;"), display
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, out) == (0, f"{pane_id} {window_id}\n"), err
    assert _show_pane_option(tmux_server, pane_id, "@termctl_name") == "n;"


def test_f5_wrong_id_is_a_nomatch_and_changes_nothing(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    cmd = tb.guarded(
        tb.pane(pane_id), tb.owned(OTHER_ID), command("kill-pane").target(tb.pane(pane_id)).build()
    )
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, out) == (0, "termctl-nomatch\n"), err
    assert _guard(tmux_server, cmd) is GuardOutcome.NOMATCH
    assert tmux_server.alive()


def test_f5_untagged_pane_is_a_nomatch(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "@termctl_marker", "1"))
    assert _guard(tmux_server, cmd) is GuardOutcome.NOMATCH
    assert _show_pane_option(tmux_server, pane_id, "@termctl_marker") == ""


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
    assert _guard(tmux_server, cmd) is GuardOutcome.NOMATCH
    assert tmux_server.raw("list-panes", "-t", other_pane).returncode == 0


def test_f5_missing_pane_is_a_nomatch(tmux_server: TmuxServer) -> None:
    tmux_server.start()
    cmd = tb.guarded(
        tb.pane("%999"), tb.owned(ID), command("kill-pane").target(tb.pane("%999")).build()
    )
    assert _guard(tmux_server, cmd) is GuardOutcome.NOMATCH


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
    assert _guard(tmux_server, cmd) is GuardOutcome.NOMATCH
    assert tmux_server.raw("list-panes", "-t", new_pane).returncode == 0


def test_f5_rename_guard_refuses_a_split_window(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    _tag(tmux_server, pane_id)
    tmux_server.raw("rename-window", "-t", window_id, "before")
    tmux_server.raw("split-window", "-d", "-t", pane_id, "--", "/usr/bin/env", "--", "sleep", "600")
    rename = command("rename-window").target(tb.window(window_id)).name("after").build()
    guarded = tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    assert _guard(tmux_server, guarded) is GuardOutcome.NOMATCH
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
    guarded = tb.guarded(tb.pane(pane_id), tb.owned_single_pane_window(ID), rename)
    assert _guard(tmux_server, guarded) is GuardOutcome.NOMATCH


def test_f5_window_target_outside_the_guarded_pane_is_a_nomatch(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    other_pane, other_window = _new_window(tmux_server)
    tmux_server.raw("rename-window", "-t", other_window, "before")
    rename = command("rename-window").target(tb.window(other_window)).name("after").build()
    assert _guard(tmux_server, tb.guarded(tb.pane(pane_id), tb.owned(ID), rename)) is (
        GuardOutcome.NOMATCH
    )
    assert _display(tmux_server, other_pane, "#{window_name}") == "before"


def test_f5_failing_inner_command_is_a_failure_not_a_match(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    _tag(tmux_server, pane_id)
    # An invalid value: the guard matches and the inner command fails.
    cmd = tb.guarded(tb.pane(pane_id), tb.owned(ID), _set_pane(pane_id, "remain-on-exit", "bogus"))
    rc, out, err = _run(tmux_server, cmd)
    assert (rc, out) == (1, "") and err
    assert _guard(tmux_server, cmd) is GuardOutcome.FAILED


def test_f5_no_server_is_a_failure_not_a_match(tmux_server: TmuxServer) -> None:
    cmd = tb.guarded(
        tb.pane("%0"), tb.owned(ID), command("kill-pane").target(tb.pane("%0")).build()
    )
    rc, out, _ = _run(tmux_server, cmd)
    assert (rc, out) == (1, "")
    assert _guard(tmux_server, cmd) is GuardOutcome.FAILED


def test_untagged_and_cleanup_guards(tmux_server: TmuxServer) -> None:
    pane_id = tmux_server.start()
    window_id = _display(tmux_server, pane_id, "#{window_id}")
    mark = _set_pane(pane_id, "@termctl_labels", "seen=1")
    pane = tb.pane(pane_id)
    nomatch, matched = GuardOutcome.NOMATCH, GuardOutcome.MATCHED
    assert _guard(tmux_server, tb.guarded(pane, tb.untagged_in_window("@999"), mark)) is nomatch
    assert _guard(tmux_server, tb.guarded(pane, tb.untagged_in_window(window_id), mark)) is matched
    _tag(tmux_server, pane_id)
    assert _guard(tmux_server, tb.guarded(pane, tb.untagged_in_window(window_id), mark)) is nomatch
    assert _guard(tmux_server, tb.guarded(pane, tb.cleanup(window_id), mark)) is matched
    # Tagged by someone else (another pane id): the cleanup kill refuses.
    tmux_server.raw("set-option", "-p", "-t", pane_id, "@termctl_pane", "%999")
    assert _guard(tmux_server, tb.guarded(pane, tb.cleanup(window_id), mark)) is nomatch


def _window_option(server: TmuxServer, window_id: str, name: str) -> str:
    """The window's own value of ``name``, empty when only inherited."""
    return server.raw("show-options", "-w", "-q", "-v", "-t", window_id, name).stdout.strip()


def test_guarded_tagging_sets_the_window_rename_options(tmux_server: TmuxServer) -> None:
    # Open order step 6: set -w -t %N inside the tagging guard acts on the pane's own window
    # (review round 3, R3-1).
    pane_id, window_id = _new_window_in_started(tmux_server)
    pane = tb.pane(pane_id)

    def tagging(window: str) -> Command:
        return tb.guarded(
            pane,
            tb.untagged_in_window(window),
            _set_pane(pane_id, "@termctl_pane", pane_id),
            command("set").flag("-w").target(pane).arg("automatic-rename").arg("off").build(),
            command("set").flag("-w").target(pane).arg("allow-rename").arg("off").build(),
            _set_pane(pane_id, "remain-on-exit", "failed"),
        )

    # A non-match (another window) changes nothing.
    assert _guard(tmux_server, tagging("@999")) is GuardOutcome.NOMATCH
    assert _window_option(tmux_server, window_id, "automatic-rename") == ""
    assert _window_option(tmux_server, window_id, "allow-rename") == ""
    assert _show_pane_option(tmux_server, pane_id, "@termctl_pane") == ""
    assert _guard(tmux_server, tagging(window_id)) is GuardOutcome.MATCHED
    assert _window_option(tmux_server, window_id, "automatic-rename") == "off"
    assert _window_option(tmux_server, window_id, "allow-rename") == "off"
    assert _show_pane_option(tmux_server, pane_id, "remain-on-exit") == "failed"
    # The other window is untouched.
    other_window = _display(tmux_server, "=base:", "#{window_id}")
    assert other_window != window_id
    assert _window_option(tmux_server, other_window, "automatic-rename") == ""


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


def test_is_no_server_matches_the_socket_path_tmux_prints(tmux_server: TmuxServer) -> None:
    """tmux names the -S path exactly as given (no realpath), here for both no-server texts."""
    missing = tmux_server.raw("list-panes", "-a")
    assert tb.is_no_server(missing, tmux_server.socket_path), missing.stderr
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(tmux_server.socket_path)
    stale.close()
    refused = tmux_server.raw("list-panes", "-a")
    assert tb.is_no_server(refused, tmux_server.socket_path), refused.stderr
