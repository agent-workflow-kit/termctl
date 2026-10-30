"""Unit tests for the tmux argument builder, guarded writes, chains and the runner
(interface.md "tmux argument rules"). Real-tmux tests are in test_tmux_integration.py."""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

from termctl import tmux_backend as tb
from termctl.errors import TermctlError
from termctl.tmux_backend import Command, Format, GuardOutcome, Invocation, Tmux, command

from .conftest import tmux_missing_outcome
from .fakes import FakeGateway

TMUX = "/usr/bin/tmux"
SOCK = "/tmp/tmux-1000/default"
ID = "tc-abcdefgh"

HOSTILE = [
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
    "#(touch /tmp/pwned)",
    "a # b",
    "'",
    "''",
    "a'b",
    '"',
    "$HOME",
    "~",
    "{}",
    "}",
    "a,b",
    "a b",
    " ",
    "\t",
    "a\nb",
    "\n",
    "\x1b[31m",
    "\x7f\x85",
    "é中\U0001f514",
    "‮",
    "x" * 5000,
    # Newlines next to whitespace, "#" and backslashes (review round 1, R1-1).
    "a\n  b",
    "if 1:\n    print(1)",
    "a\\\nb",
    "a\\\\\\\nb",
    "\\\n",
    "a\\\n'b",
    "x\n set -g @pwned 1",
    "a\r\nb",
    "a\n#x",
    "a\n\tb",
    "\n\n",
]


# --- rule 1: separators -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a;", "a\\;"),
        (";", "\\;"),
        (";;", ";\\;"),
        ("a\\;", "a\\\\;"),
        ("a;b", "a;b"),
        ("a", "a"),
        ("", ""),
        ("a; ", "a; "),
    ],
)
def test_escape_separator_replaces_only_the_final_semicolon(value: str, expected: str) -> None:
    assert tb.escape_separator(value) == expected


@pytest.mark.parametrize("value", HOSTILE)
def test_no_data_element_ends_in_a_bare_semicolon(value: str) -> None:
    cmd = (
        command("set-option")
        .flag("-p")
        .target(tb.pane("%1"))
        .arg("@termctl_name")
        .arg(value + ";")
        .build()
    )
    argv = Invocation.of(cmd).argv(TMUX, SOCK)
    # The only bare separators are termctl's own; one command, so none.
    assert ";" not in argv
    assert all(not e.endswith(";") or e.endswith("\\;") for e in argv)


def test_semicolon_rule_applies_in_every_position() -> None:
    cmd = (
        command("new-window")
        .flag("-d")
        .target(tb.session("work"))
        .window_name("n;")
        .option("-c", "/d;")
        .env("A", "1;")
        .arg("/usr/bin/env")
        .arg("--")
        .arg("x;")
        .build()
    )
    assert cmd.argv == (
        "new-window",
        "-d",
        "-t",
        "=work:",
        "-n",
        "n\\;",
        "-c",
        "/d\\;",
        "-e",
        "A=1\\;",
        "--",
        "/usr/bin/env",
        "--",
        "x\\;",
    )


# --- rule 2: leading "-" ------------------------------------------------------------------------


def test_double_dash_precedes_the_first_positional_only() -> None:
    cmd = command("set-option").flag("-p").target(tb.pane("%3")).arg("@a").arg("-v").build()
    assert cmd.argv == ("set-option", "-p", "-t", "%3", "--", "@a", "-v")


def test_no_double_dash_without_positionals() -> None:
    assert command("kill-pane").target(tb.pane("%3")).build().argv == ("kill-pane", "-t", "%3")


def test_a_positional_double_dash_is_data() -> None:
    cmd = command("respawn-pane").arg("/usr/bin/env").arg("--").arg("ls").build()
    assert cmd.argv == ("respawn-pane", "--", "/usr/bin/env", "--", "ls")


def test_flags_after_a_positional_are_refused() -> None:
    builder = command("rename-window").name("x")
    with pytest.raises(ValueError):
        builder.flag("-d")
    with pytest.raises(ValueError):
        builder.option("-c", "/x")
    with pytest.raises(ValueError):
        builder.window_name("x")
    with pytest.raises(ValueError):
        builder.target(tb.window("@1"))
    with pytest.raises(ValueError):
        builder.env("A", "1")


@pytest.mark.parametrize("flag", ["", "-", "--", "-ab", "x", "-1", "--json", "-;", " -d"])
def test_malformed_flags_are_refused(flag: str) -> None:
    with pytest.raises(ValueError):
        command("kill-pane").flag(flag)
    with pytest.raises(ValueError):
        command("kill-pane").option(flag, "v")


@pytest.mark.parametrize("flag", ["-t", "-e", "-F"])
def test_flags_with_their_own_methods_are_refused_as_plain_options(flag: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").option(flag, "x")


@pytest.mark.parametrize("flag", ["-t", "-e", "-F", "-n", "-c", "-s", "-f"])
def test_value_taking_flags_are_refused_without_a_value(flag: str) -> None:
    with pytest.raises(ValueError):
        command("set-option").flag(flag)


def test_filter_option_needs_a_format() -> None:
    with pytest.raises(ValueError):
        command("list-panes").option("-f", "#{pane_dead}")
    fmt = tb.fields("pane_id")
    assert command("list-panes").format("-f", fmt).build().argv == (
        "list-panes",
        "-f",
        "#{pane_id}",
    )


@pytest.mark.parametrize("flag", ["-t", "-e", "-n", "-c", "-s", "-x"])
def test_formats_go_only_to_format_flags(flag: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").format(flag, tb.fields("pane_id"))


def test_server_scope_is_set_options_s_flag() -> None:
    cmd = command("set-option").server_scope().arg("exit-unattached").arg("off").build()
    assert cmd.argv == ("set-option", "-s", "--", "exit-unattached", "off")
    with pytest.raises(ValueError):
        command("new-window").server_scope()


@pytest.mark.parametrize("name", ["", "-x", "set;", "Set", "a b", "new-window;", "#{x}"])
def test_malformed_command_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError):
        command(name)


# --- rule 3: format expansion -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("#", "##"),
        ("##", "####"),
        ("a#b", "a##b"),
        ("#(touch x)", "##(touch x)"),
        ("#{pane_id}", "##{pane_id}"),
        ("PR#42 review", "PR##42 review"),
        ("no hash", "no hash"),
        ("#;", "##\\;"),
    ],
)
def test_format_expanded_values_double_every_hash(value: str, expected: str) -> None:
    assert command("new-session").option("-c", value).build().argv == (
        "new-session",
        "-c",
        expected,
    )
    assert command("new-session").window_name(value).build().argv == (
        "new-session",
        "-n",
        expected,
    )
    assert command("rename-window").name(value).build().argv == ("rename-window", "--", expected)


@pytest.mark.parametrize("value", ["#[fg=red]", "a#[b", "##[", "#["])
def test_status_styles_are_refused_in_format_expanded_values(value: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").window_name(value)
    with pytest.raises(ValueError):
        command("new-window").option("-c", value)
    with pytest.raises(ValueError):
        command("rename-window").name(value)
    with pytest.raises(ValueError):
        command("rename-window").arg(value)


def test_rename_window_arg_is_format_expanded_like_name() -> None:
    cmd = command("rename-window").target(tb.window("@2")).arg("a#(b)").build()
    assert cmd.argv == ("rename-window", "-t", "@2", "--", "a##(b)")


@pytest.mark.parametrize("name", ["display", "display-message"])
def test_display_positional_must_be_a_format(name: str) -> None:
    with pytest.raises(ValueError):
        command(name).flag("-p").arg("#(touch x)")
    assert command(name).flag("-p").format_arg(tb.fields("pane_id")).build().argv[-1] == (
        "#{pane_id}"
    )


@pytest.mark.parametrize("value", ["#[fg=red]", "#(x)", "#{y}", "a;"])
def test_literal_positions_are_not_doubled(value: str) -> None:
    cmd = (
        command("set-option")
        .flag("-p")
        .target(tb.pane("%1"))
        .env("A", value)
        .arg("@o")
        .arg(value)
        .build()
    )
    assert cmd.argv[5] == "A=" + tb.escape_separator(value)
    assert cmd.argv[-1] == tb.escape_separator(value)


def test_formats_are_passed_verbatim() -> None:
    fmt = tb.fields("pane_id", "window_id")
    cmd = command("new-window").flag("-P").format("-F", fmt).build()
    assert cmd.argv == ("new-window", "-P", "-F", "#{pane_id} #{window_id}")
    assert command("display").flag("-p").format_arg(fmt).build().argv[-1] == fmt.text


@pytest.mark.parametrize("text", ["#{pane_id}", "#(touch x)", "a;", "\x00"])
def test_formats_cannot_be_built_directly(text: str) -> None:
    with pytest.raises(TypeError):
        Format(text)


def test_fields_builds_a_format_of_field_names() -> None:
    assert tb.fields("pane_id", "@termctl_name").text == "#{pane_id} #{@termctl_name}"


@pytest.mark.parametrize(
    "name", ["", "#(x)", "a}", "a,b", "a b", "pane_id;", "Pane", "@", "1a", "a\n"]
)
def test_fields_refuses_anything_but_field_names(name: str) -> None:
    with pytest.raises(ValueError):
        tb.fields(name)
    with pytest.raises(ValueError):
        tb.fields()


@pytest.mark.parametrize("value", ["\ud800", "a\udbff", "\udfff", "\udc7f"])
def test_lone_surrogates_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        command("x").arg(value)
    with pytest.raises(ValueError):
        command("x").env("A", value)


def test_surrogate_escaped_bytes_are_accepted() -> None:
    # os.fsdecode writes an undecodable byte as \udc80-\udcff; it encodes back to the byte.
    assert command("x").arg("a\udc80\udcff").build().argv[-1] == "a\udc80\udcff"


def test_nul_is_refused_everywhere() -> None:
    with pytest.raises(ValueError):
        command("x").arg("a\x00b")
    with pytest.raises(ValueError):
        command("x").option("-n", "a\x00")
    with pytest.raises(ValueError):
        command("x").env("A", "\x00")


# --- exact targets ------------------------------------------------------------------------------


def test_targets_are_exact() -> None:
    assert tb.pane("%12").text == "%12"
    assert tb.window("@7").text == "@7"
    assert tb.session("work").text == "=work:"
    assert tb.pane_in_session("work", "@7", "%12").text == "=work:@7.%12"


@pytest.mark.parametrize("value", ["", "%", "12", "%1a", "%-1", "%1;", " %1", "%1 ", "@1", "%１"])
def test_bad_pane_targets_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        tb.pane(value)


@pytest.mark.parametrize("value", ["", "@", "1", "@1a", "%1", "@1;", "@١"])
def test_bad_window_targets_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        tb.window(value)


@pytest.mark.parametrize(
    "value", ["", "a:b", "a.b", "=a", "a;", "#{x}", "a b", "-a" + "x" * 64, "é", "x" * 65]
)
def test_bad_session_targets_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        tb.session(value)
    with pytest.raises(ValueError):
        tb.pane_in_session(value, "@1", "%1")


def test_target_cannot_be_forged_from_a_string() -> None:
    with pytest.raises(TypeError):
        command("kill-pane").target("%1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text", ["main; run-shell x", "main", "=a", "%1;", "=a:@1", "=a:%1", "@1.%2", "", "=a b:"]
)
def test_targets_built_directly_are_checked(text: str) -> None:
    with pytest.raises(ValueError):
        tb.Target(text)


def test_targets_built_directly_with_a_valid_grammar_are_accepted() -> None:
    assert tb.Target("%3") == tb.pane("%3")
    assert tb.Target("=w:@1.%2") == tb.pane_in_session("w", "@1", "%2")


def test_targets_know_their_kind() -> None:
    assert tb.pane("%1").kind == "pane"
    assert tb.window("@1").kind == "window"
    assert tb.session("s").kind == "session"
    assert tb.pane_in_session("s", "@1", "%1").kind == "pane_in_session"


def test_a_command_takes_one_target() -> None:
    with pytest.raises(ValueError):
        command("kill-pane").target(tb.pane("%1")).target(tb.pane("%2"))


def test_a_command_records_its_target() -> None:
    assert command("kill-pane").target(tb.pane("%1")).build().target == tb.pane("%1")
    assert command("kill-pane").build().target is None


# --- -e and redaction ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "1A", "A-B", "A=B", "A B", "A;"])
def test_env_names_follow_the_grammar(name: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").env(name, "v")
    with pytest.raises(ValueError):
        command("respawn-pane").env_arg(name, "v")


def test_env_values_are_shown_by_name_only() -> None:
    cmd = (
        command("respawn-pane")
        .flag("-k")
        .env("TOKEN", "s3cret;")
        .arg("/usr/bin/env")
        .arg("--")
        .env_arg("PATH", "/secret/bin")
        .arg("ls")
        .build()
    )
    assert "-e" in cmd.argv and "TOKEN=s3cret\\;" in cmd.argv and "PATH=/secret/bin" in cmd.argv
    assert cmd.shown == (
        "respawn-pane",
        "-k",
        "-e",
        "TOKEN",
        "--",
        "/usr/bin/env",
        "--",
        "PATH",
        "ls",
    )
    assert not any("s3cret" in e or "/secret" in e for e in cmd.shown)


# --- chains -------------------------------------------------------------------------------------


def test_chain_joins_commands_with_bare_separators() -> None:
    a = command("set-option").server_scope().arg("exit-unattached").arg("off").build()
    b = command("new-session").flag("-d").session_name("work").build()
    inv = Invocation.of(a, b)
    assert inv.argv(TMUX, SOCK) == [
        TMUX,
        "-S",
        SOCK,
        "set-option",
        "-s",
        "--",
        "exit-unattached",
        "off",
        ";",
        "new-session",
        "-d",
        "-s",
        "work",
    ]


def test_chain_needs_a_command() -> None:
    with pytest.raises(ValueError):
        Invocation.of()


@pytest.mark.parametrize("tmux_bin", ["tmux", "bin/tmux", ""])
def test_tmux_bin_must_be_absolute(tmux_bin: str) -> None:
    with pytest.raises(ValueError):
        Invocation.of(command("list-panes").build()).argv(tmux_bin, SOCK)


@pytest.mark.parametrize("socket", ["", "rel/sock", "default", "/a/b;", "/a/\nb", "/a,b"])
def test_socket_path_must_be_absolute_and_safe(socket: str) -> None:
    with pytest.raises(ValueError):
        Invocation.of(command("list-panes").build()).argv(TMUX, socket)


def test_shown_argv_redacts_env_values() -> None:
    inv = Invocation.of(command("new-window").env("A", "v").build())
    assert inv.shown_argv(TMUX, SOCK) == [TMUX, "-S", SOCK, "new-window", "-e", "A"]


# --- rule 5: guarded writes ---------------------------------------------------------------------


def test_owned_condition_matches_the_interface() -> None:
    assert tb.owned(ID).text == (
        "#{&&:#{==:#{@termctl_pane},#{pane_id}},#{==:#{@termctl_id},tc-abcdefgh}}"
    )


@pytest.mark.parametrize("value", ["", "tc-ABCDEFGH", "tc-abcdefg", "tc-abcdefgh,", "tc-abcd}fgh"])
def test_owned_condition_refuses_bad_ids(value: str) -> None:
    with pytest.raises(ValueError):
        tb.owned(value)


def test_rename_condition_adds_window_and_single_pane() -> None:
    assert tb.owned_single_pane_window(ID).text == (
        "#{&&:#{==:#{@termctl_pane},#{pane_id}},#{&&:#{==:#{@termctl_id},tc-abcdefgh},"
        "#{&&:#{==:#{window_id},#{@termctl_window}},#{==:#{window_panes},1}}}}"
    )


def test_untagged_and_cleanup_conditions() -> None:
    assert tb.untagged_in_window("@4").text == (
        "#{&&:#{==:#{@termctl_pane},},#{==:#{window_id},@4}}"
    )
    assert tb.cleanup("@4").text == (
        "#{&&:#{==:#{window_id},@4},#{||:#{==:#{@termctl_pane},},"
        "#{==:#{@termctl_pane},#{pane_id}}}}"
    )
    with pytest.raises(ValueError):
        tb.untagged_in_window("%4")
    with pytest.raises(ValueError):
        tb.cleanup("@4,1")


def test_all_of_needs_two_conditions() -> None:
    with pytest.raises(ValueError):
        tb.all_of(tb.owned(ID))


def test_guard_shape() -> None:
    inner = command("kill-pane").target(tb.pane("%5")).build()
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), inner)
    assert cmd.argv == (
        "if-shell",
        "-F",
        "-t",
        "%5",
        "--",
        tb.owned(ID).text,
        "'kill-pane' '-t' '%5'",
        "'display' '-p' '--' 'termctl-nomatch'",
    )


def test_guard_needs_an_inner_command() -> None:
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID))


def test_guard_quotes_each_element_without_separator_escaping() -> None:
    inner = (
        command("set-option")
        .flag("-p")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .arg("it's; #(x) $HOME ~ \\ ;")
        .build()
    )
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), inner)
    assert cmd.argv[6] == (
        "'set-option' '-p' '-t' '%5' '--' '@termctl_name' 'it'\\''s; #(x) $HOME ~ \\ ;'"
    )


def test_guard_chains_inner_commands() -> None:
    a = command("set-option").flag("-p").target(tb.pane("%5")).arg("@termctl_name").arg("1").build()
    b = (
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(tb.fields("pane_pid"))
        .build()
    )
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), a, b)
    assert cmd.argv[6] == (
        "'set-option' '-p' '-t' '%5' '--' '@termctl_name' '1' ; "
        "'display' '-p' '-t' '%5' '--' '#{pane_pid}'"
    )


def test_guard_inner_rename_doubles_hashes_before_quoting() -> None:
    inner = command("rename-window").target(tb.window("@2")).name("a#(b)").build()
    cmd = tb.guarded(tb.pane("%5"), tb.owned_single_pane_window(ID), inner)
    assert cmd.argv[6] == "'rename-window' '-t' '@2' '--' 'a##(b)'"


def test_guard_on_a_window_target_requires_the_pane_to_be_in_it() -> None:
    inner = command("rename-window").target(tb.window("@2")).name("x").build()
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), inner)
    assert cmd.argv[5] == tb.all_of(tb.owned(ID), tb.in_window("@2")).text


def test_guard_target_spoofs_are_refused() -> None:
    guard, cond = tb.pane("%5"), tb.owned(ID)
    spoofs = [
        # Another pane, a session or a pane-in-session target.
        command("kill-pane").target(tb.pane("%6")).build(),
        command("kill-session").target(tb.session("s")).build(),
        command("kill-pane").target(tb.pane_in_session("s", "@1", "%5")).build(),
        # if-shell inside a guard, however built.
        command("if-shell").target(guard).arg("1").arg("kill-server").build(),
    ]
    for inner in spoofs:
        with pytest.raises(ValueError):
            tb.guarded(guard, cond, inner)
    with pytest.raises(ValueError):
        command("kill-pane").flag("-t")
    # An option value "-t" is refused as flag-like, so it cannot pose as a target (R4-1).
    with pytest.raises(ValueError):
        command("respawn-pane").option("-c", "-t")


def _set_p(name: str, value: str, target: tb.Target | None = None) -> Command:
    return command("set").flag("-p").target(target or tb.pane("%5")).arg(name).arg(value).build()


def _set_w(name: str, value: str, target: tb.Target | None = None) -> Command:
    return command("set").flag("-w").target(target or tb.pane("%5")).arg(name).arg(value).build()


def _step6_tagging() -> tuple[Command, ...]:
    """Open order step 6 with argv: the pane options, the window options automatic-rename off
    and allow-rename off, remain-on-exit, respawn-pane -k and the query of step 7."""
    pane_options = [
        ("@termctl_v", "1"),
        ("@termctl_pane", "%5"),
        ("@termctl_window", "@2"),
        ("@termctl_id", ID),
        ("@termctl_name", "PR#42 review"),
        ("@termctl_marker", ""),
        ("@termctl_marker_text", ""),
        ("@termctl_labels", "a=b"),
        ("@termctl_placement", "window"),
        ("@termctl_keep", "failed"),
        ("@termctl_created", "2026-10-30T00:00:00.000Z"),
        ("@termctl_pid", ""),
        ("@termctl_pid_start", ""),
    ]
    return (
        *(_set_p(name, value) for name, value in pane_options),
        _set_w("automatic-rename", "off"),
        _set_w("allow-rename", "off"),
        _set_p("remain-on-exit", "failed"),
        command("respawn-pane")
        .flag("-k")
        .option("-c", "/tmp")
        .target(tb.pane("%5"))
        .env("V", "v")
        .arg("/usr/bin/env")
        .arg("--")
        .arg("sleep")
        .arg("30")
        .build(),
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(tb.fields("pane_pid", "pane_dead", "pid"))
        .build(),
    )


def test_guard_builds_every_guarded_sequence_of_the_spec() -> None:
    # Every guarded sequence of interface.md, each as one guard (review round 3, R3-1).
    pane5, window2 = tb.pane("%5"), tb.window("@2")
    kill = command("kill-pane").target(pane5).build()
    sequences: list[tuple[Format, tuple[Command, ...]]] = [
        # Open order step 6 with argv (the tagging invocation).
        (tb.untagged_in_window("@2"), _step6_tagging()),
        # Open order step 6 without argv: the same options, then the query of step 7.
        (
            tb.untagged_in_window("@2"),
            tuple(c for c in _step6_tagging() if c.name != "respawn-pane"),
        ),
        # The kill after a failed tagging (Open order).
        (tb.cleanup("@2"), (kill,)),
        # rename, mark, unmark: the pane options, then the window rename.
        (
            tb.owned(ID),
            (
                _set_p("@termctl_name", "n"),
                _set_p("@termctl_marker", "attention"),
                _set_p("@termctl_marker_text", "\U0001f514 "),
            ),
        ),
        (
            tb.owned_single_pane_window(ID),
            (command("rename-window").target(window2).name("\U0001f514 n").build(),),
        ),
        # label add / remove.
        (tb.owned(ID), (_set_p("@termctl_labels", "a=b c=d"),)),
        # close of a dead pane; close --force reads the PID and kills in one guard.
        (tb.owned(ID), (kill,)),
        (
            tb.owned(ID),
            (
                command("display")
                .flag("-p")
                .target(pane5)
                .format_arg(tb.fields("pane_pid"))
                .build(),
                kill,
            ),
        ),
    ]
    for condition, inners in sequences:
        built = tb.guarded(pane5, condition, *inners)
        assert built.guarded
        assert built.elements[-2] == " ; ".join(c.quoted() for c in inners)


def test_guard_accepts_the_spelled_out_forms() -> None:
    guard, cond, pane5 = tb.pane("%5"), tb.owned(ID), tb.pane("%5")
    inners = [
        command("rename-window").target(pane5).name("n").build(),
        command("set-option").flag("-p").target(pane5).arg("@termctl_name").arg("1").build(),
        command("set-option").flag("-p").flag("-u").target(pane5).arg("@termctl_name").build(),
        command("set-option")
        .flag("-p")
        .flag("-q")
        .flag("-o")
        .target(pane5)
        .arg("@termctl_name")
        .arg("1")
        .build(),
        command("set-option").flag("-w").target(pane5).arg("allow-rename").arg("off").build(),
        command("display-message")
        .flag("-p")
        .target(pane5)
        .format_arg(tb.fields("pane_pid"))
        .build(),
    ]
    for inner in inners:
        assert tb.guarded(guard, cond, inner).guarded


def test_guard_window_option_on_the_pane_needs_no_window_condition() -> None:
    # set -w -t %N acts on the window containing the guarded pane itself.
    cond = tb.untagged_in_window("@2")
    built = tb.guarded(tb.pane("%5"), cond, _set_w("automatic-rename", "off"))
    assert built.elements[built.elements.index("--") + 1] == cond.text


def test_guard_window_option_on_a_window_requires_containment() -> None:
    # set -w -t @W: like rename-window -t @W, the condition also requires the pane in @W.
    cond = tb.owned(ID)
    built = tb.guarded(tb.pane("%5"), cond, _set_w("allow-rename", "off", tb.window("@3")))
    assert (
        built.elements[built.elements.index("--") + 1] == tb.all_of(cond, tb.in_window("@3")).text
    )


@pytest.mark.parametrize(
    "inner",
    [
        # Acting on panes or windows other than the guarded pane (review round 2, R2-2).
        command("kill-pane").flag("-a").target(tb.pane("%5")).build(),
        command("kill-window").target(tb.window("@1")).build(),
        command("kill-window").target(tb.pane("%5")).build(),
        command("swap-pane").target(tb.pane("%5")).build(),
        command("respawn-window").flag("-k").target(tb.window("@1")).build(),
        command("new-window").target(tb.pane("%5")).build(),
        # A window target is for rename-window only.
        command("kill-pane").target(tb.window("@1")).build(),
        command("set-option")
        .flag("-p")
        .target(tb.window("@1"))
        .arg("@termctl_name")
        .arg("1")
        .build(),
        command("display").flag("-p").target(tb.window("@1")).format_arg(tb.fields("a")).build(),
        command("respawn-pane").flag("-k").target(tb.window("@1")).build(),
        # set only at pane scope: not global, server, window or session options.
        command("set-option").target(tb.pane("%5")).arg("@termctl_name").arg("1").build(),
        command("set-option")
        .flag("-g")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .arg("1")
        .build(),
        command("set-option")
        .flag("-w")
        .flag("-g")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .build(),
        command("set-option")
        .flag("-w")
        .flag("-a")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .build(),
        command("set-option")
        .flag("-w")
        .flag("-U")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .build(),
        command("set").server_scope().flag("-w").target(tb.pane("%5")).arg("@termctl_name").build(),
        command("set")
        .flag("-p")
        .flag("-w")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .arg("1")
        .build(),
        command("set").flag("-w").target(tb.pane("%6")).arg("allow-rename").arg("off").build(),
        command("set").flag("-w").target(tb.session("s")).arg("allow-rename").arg("off").build(),
        command("set-option")
        .flag("-p")
        .flag("-g")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .build(),
        command("set").server_scope().flag("-p").target(tb.pane("%5")).arg("@termctl_name").build(),
        command("set-option")
        .flag("-p")
        .flag("-a")
        .target(tb.pane("%5"))
        .arg("@termctl_name")
        .build(),
        # -a and -s refused on any inner command, as a flag or an option.
        command("respawn-pane").flag("-a").target(tb.pane("%5")).build(),
        command("display").flag("-a").flag("-p").target(tb.pane("%5")).build(),
        command("rename-window").flag("-a").target(tb.window("@1")).name("n").build(),
    ],
    ids=lambda c: " ".join(c.elements),
)
def test_guard_refuses_inner_commands_that_act_outside_the_pane(inner: Command) -> None:
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), inner)


def test_a_command_records_its_flags() -> None:
    cmd = (
        command("respawn-pane")
        .flag("-k")
        .option("-c", "/a")
        .target(tb.pane("%5"))
        .env("V", "-s")
        .arg("-g")
        .build()
    )
    assert cmd.flags == ("-k", "-c", "-t", "-e")
    # An -e value or positional that looks like a flag is data, not a flag.
    assert tb.guarded(tb.pane("%5"), tb.owned(ID), cmd).guarded


def test_an_option_value_double_dash_is_refused() -> None:
    with pytest.raises(ValueError):
        command("respawn-pane").option("-c", "--")


@pytest.mark.parametrize(
    "outer", [tb.window("@1"), tb.session("s"), tb.pane_in_session("s", "@1", "%5")]
)
def test_guard_target_must_be_a_pane(outer: tb.Target) -> None:
    inner = command("kill-pane").target(tb.pane("%5")).build()
    with pytest.raises(ValueError):
        tb.guarded(outer, tb.owned(ID), inner)


def test_guard_shown_redacts_inner_env_values() -> None:
    inner = command("respawn-pane").flag("-k").target(tb.pane("%5")).env("K", "secret").build()
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), inner)
    assert "secret" in cmd.argv[6]
    assert all("secret" not in e for e in cmd.shown)
    assert cmd.shown[6] == "'respawn-pane' '-k' '-t' '%5' '-e' 'K'"


def test_guard_refuses_inner_commands_without_a_target() -> None:
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), command("kill-pane").build())
    # A "-t" among the positionals is data, not a target.
    no_target = command("set-option").flag("-p").arg("-t").arg("%5").build()
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), no_target)


def test_guard_cannot_nest_guards() -> None:
    inner = tb.guarded(
        tb.pane("%5"), tb.owned(ID), command("kill-pane").target(tb.pane("%5")).build()
    )
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), inner)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "''"),
        ("a b", "'a b'"),
        ("it's", "'it'\\''s'"),
        ("a\nb", "'a'\\n'b'"),
        ("\n", "''\\n''"),
        ("a\n  b", "'a'\\n'  b'"),
        ("a\\\nb", "'a\\'\\n'b'"),
        ("a\n#x", "'a'\\n'#x'"),
        # Byte 0xff (os.fsdecode's "\udcff") ends tmux's parse even inside quotes; "\377"
        # outside the quotes is that byte (review round 2, R2-1).
        ("a\udcffb", "'a'\\377'b'"),
        ("\udcff", "''\\377''"),
        ("a\udcff; set -g @pwned 1", "'a'\\377'; set -g @pwned 1'"),
        ("\udcff\n\udcff", "''\\377''\\n''\\377''"),
        # Other undecodable bytes stay inside the quotes.
        ("\udc80\udcc3;\udcfe", "'\udc80\udcc3;\udcfe'"),
    ],
)
def test_quote_writes_newlines_and_0xff_as_escapes_outside_the_quotes(
    value: str, expected: str
) -> None:
    # tmux's parser drops "\" + newline anywhere, and blanks and "#..." comments after a
    # newline even inside quotes; "\n" outside quotes is a newline (spike F5). The real-tmux
    # round trips are in test_tmux_integration.py.
    assert tb.quote(value) == expected


@pytest.mark.parametrize("value", [*HOSTILE, "\udcff", "a\udcff\udcffb\udcff"])
def test_quote_leaves_no_raw_newline_or_0xff(value: str) -> None:
    quoted = tb.quote(value)
    assert "\n" not in quoted
    assert "\udcff" not in quoted


def _result(rc: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tmux"], rc, stdout, "")


@pytest.mark.parametrize(
    ("rc", "stdout", "outcome"),
    [
        (0, "", GuardOutcome.MATCHED),
        (0, "123 0 456\n", GuardOutcome.MATCHED),
        (0, "termctl-nomatch\n", GuardOutcome.NOMATCH),
        (0, "termctl-nomatch", GuardOutcome.NOMATCH),
        (0, "x\ntermctl-nomatch\n", GuardOutcome.NOMATCH),
        (0, "termctl-nomatch-not\n", GuardOutcome.MATCHED),
        (0, " termctl-nomatch\n", GuardOutcome.MATCHED),
        (1, "", GuardOutcome.FAILED),
        (1, "123\n", GuardOutcome.FAILED),
        (1, "termctl-nomatch\n", GuardOutcome.FAILED),
        (-9, "", GuardOutcome.FAILED),
    ],
)
def test_guard_outcome(rc: int, stdout: str, outcome: GuardOutcome) -> None:
    assert tb.guard_outcome(_result(rc, stdout)) is outcome


def test_check_guard_raises_not_found_on_a_nomatch() -> None:
    with pytest.raises(TermctlError) as info:
        tb.check_guard(_result(0, "termctl-nomatch\n"), "tc-abcdefgh")
    assert info.value.code == "not_found" and info.value.exit_code == 10
    tb.check_guard(_result(0, ""), "tc-abcdefgh")


def test_check_guard_never_treats_a_failure_as_a_match() -> None:
    failed = _result(1, "")
    with pytest.raises(tb.TmuxCommandFailed) as info:
        tb.check_guard(failed, "tc-abcdefgh")
    assert info.value.result is failed


# --- the runner ---------------------------------------------------------------------------------


def _done(
    argv: list[str], rc: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, err)


class RecordingGateway(FakeGateway):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.envs: list[dict[str, str] | None] = []
        self.timeouts: list[float | None] = []

    def run(self, argv, *, stdin=None, timeout=None, env=None):  # type: ignore[no-untyped-def,override]
        self.envs.append(None if env is None else dict(env))
        self.timeouts.append(timeout)
        return super().run(argv, stdin=stdin, timeout=timeout, env=env)


def test_runner_runs_argv_with_the_given_env_and_traces_redacted() -> None:
    inv = Invocation.of(command("new-window").env("A", "secret").build())
    argv = inv.argv(TMUX, SOCK)
    gateway = RecordingGateway({tuple(argv): _done(argv, out="%1 @1\n")})
    traced: list[str] = []
    tmux = Tmux(gateway, TMUX, SOCK, env={"PATH": "/usr/bin"}, trace=traced.append)
    result = tmux.run(inv)
    assert result.stdout == "%1 @1\n"
    assert gateway.calls == [argv]
    assert gateway.envs == [{"PATH": "/usr/bin"}]
    assert gateway.timeouts == [tb.TMUX_TIMEOUT]
    assert traced == [f"tmux: {TMUX} -S {SOCK} new-window -e A"]
    assert "secret" not in traced[0]


def test_runner_trace_quotes_hostile_elements() -> None:
    inv = Invocation.of(command("rename-window").target(tb.window("@1")).name("a b;").build())
    argv = inv.argv(TMUX, SOCK)
    gateway = RecordingGateway({tuple(argv): _done(argv)})
    traced: list[str] = []
    Tmux(gateway, TMUX, SOCK, env={}, trace=traced.append).run(inv)
    assert traced == [f"tmux: {TMUX} -S {SOCK} rename-window -t @1 -- 'a b\\;'"]


def test_runner_trace_escapes_control_characters() -> None:
    value = "a\x1b[2Jb\x07\x7f\x9b\nc"
    inv = Invocation.of(
        command("set-option").flag("-p").target(tb.pane("%1")).arg("@a").arg(value).build()
    )
    argv = inv.argv(TMUX, SOCK)
    gateway = RecordingGateway({tuple(argv): _done(argv)})
    traced: list[str] = []
    Tmux(gateway, TMUX, SOCK, env={}, trace=traced.append).run(inv)
    assert traced == [
        f"tmux: {TMUX} -S {SOCK} set-option -p -t %1 -- @a 'a\\x1b[2Jb\\x07\\x7f\\x9b\\x0ac'"
    ]


def test_runner_returns_failures_to_the_caller() -> None:
    inv = Invocation.of(command("kill-pane").target(tb.pane("%9")).build())
    argv = inv.argv(TMUX, SOCK)
    gateway = RecordingGateway({tuple(argv): _done(argv, rc=1, err="can't find pane: %9\n")})
    result = Tmux(gateway, TMUX, SOCK, env={}).run(inv)
    assert (result.returncode, result.stderr) == (1, "can't find pane: %9\n")


class RaisingGateway(FakeGateway):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    def run(self, argv, *, stdin=None, timeout=None, env=None):  # type: ignore[no-untyped-def,override]
        raise self.exc


def test_runner_maps_a_timeout_to_server_unavailable() -> None:
    inv = Invocation.of(command("list-panes").flag("-a").build())
    tmux = Tmux(RaisingGateway(subprocess.TimeoutExpired(["tmux"], 30)), TMUX, SOCK, env={})
    with pytest.raises(TermctlError) as info:
        tmux.run(inv)
    assert info.value.code == "server_unavailable"


def test_runner_maps_an_exec_failure_to_tmux_missing() -> None:
    inv = Invocation.of(command("list-panes").flag("-a").build())
    tmux = Tmux(RaisingGateway(FileNotFoundError(2, "No such file")), TMUX, SOCK, env={})
    with pytest.raises(TermctlError) as info:
        tmux.run(inv)
    assert info.value.code == "tmux_missing"


def test_runner_refuses_a_relative_binary() -> None:
    with pytest.raises(ValueError):
        Tmux(FakeGateway(), "tmux", SOCK, env={})


def test_format_cannot_be_forged_with_replace() -> None:
    # Review round 2, R2-3: replace() would otherwise copy the factory key.
    with pytest.raises(TypeError):
        dataclasses.replace(tb.fields("a"), text="#(touch /tmp/x)")
    with pytest.raises(TypeError):
        Format("#(touch /tmp/x)", tb._FORMAT_KEY)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Format(text="#{pane_id}")


def test_command_cannot_be_built_directly() -> None:
    # Review round 3, R3-3: guarded() trusts a Command's target and flags, so only
    # CommandBuilder.build() (and guarded() itself) may make one.
    with pytest.raises(TypeError):
        Command(
            ("kill-pane", "-a", "-t", "%5"), ("kill-pane",), target=tb.pane("%5"), flags=("-t",)
        )
    with pytest.raises(TypeError):
        Command(("if-shell", "-t", "%5", "--", "1", "kill-server"), ("if-shell",))


def test_command_cannot_be_forged_with_replace() -> None:
    cmd = command("kill-pane").flag("-a").target(tb.pane("%5")).build()
    with pytest.raises(TypeError):
        dataclasses.replace(cmd, flags=("-t",))
    guarded = tb.guarded(
        tb.pane("%5"), tb.owned(ID), command("kill-pane").target(tb.pane("%5")).build()
    )
    with pytest.raises(TypeError):
        dataclasses.replace(guarded, guarded=False)


def test_formats_compare_by_text() -> None:
    assert tb.fields("a") == tb.fields("a")
    assert tb.fields("a").text == "#{a}"


def test_command_is_immutable() -> None:
    cmd = command("kill-pane").target(tb.pane("%1")).build()
    assert isinstance(cmd, Command)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.elements = ()  # type: ignore[misc]


# --- the tmux_server fixture --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "outcome"),
    [({}, "skip"), ({"CI": "true"}, "fail"), ({"CI": "1"}, "fail"), ({"CI": ""}, "skip")],
)
def test_missing_tmux_fails_under_ci_and_skips_elsewhere(
    environ: dict[str, str], outcome: str
) -> None:
    assert tmux_missing_outcome(environ) == outcome


# --- review round 4 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "flag", "value"),
    [
        # A flag that takes no value: its "value" would be parsed as the next flag (R4-1).
        ("set", "-w", "-g"),
        ("set-option", "-p", "-g"),
        ("set", "-q", "-a"),
        ("set", "-p", "x"),
        ("display", "-s", "x"),
        ("swap-pane", "-s", "%7"),
        ("new-window", "-T", "x"),
        ("x", "-x", "v"),
        ("set-option", "-c", "x"),
        # -n and -s have their own methods.
        ("new-window", "-n", "x"),
        ("new-session", "-s", "x"),
    ],
)
def test_option_takes_only_the_value_taking_flags_of_its_command(
    name: str, flag: str, value: str
) -> None:
    with pytest.raises(ValueError):
        command(name).option(flag, value)


def test_flags_smuggled_as_option_values_never_reach_a_guard() -> None:
    # The reviewer's R4-1 probe: set -w -g -t %1 -- window-status-format '#(touch PWN)'.
    with pytest.raises(ValueError):
        tb.guarded(
            tb.pane("%1"),
            tb.owned(ID),
            command("set")
            .option("-w", "-g")
            .target(tb.pane("%1"))
            .arg("window-status-format")
            .arg("#(touch PWN)")
            .build(),
        )
    for flag, value in (("-p", "-g"), ("-q", "-a")):
        with pytest.raises(ValueError):
            tb.guarded(
                tb.pane("%1"),
                tb.owned(ID),
                command("set")
                .flag("-p")
                .option(flag, value)
                .target(tb.pane("%1"))
                .arg("@termctl_name")
                .arg("x")
                .build(),
            )


@pytest.mark.parametrize("value", ["-", "--", "-t", "-a", "-g", "--json"])
@pytest.mark.parametrize("name", ["new-window", "new-session", "respawn-pane", "switch-client"])
def test_option_refuses_flag_like_values(name: str, value: str) -> None:
    with pytest.raises(ValueError):
        command(name).option("-c", value)


@pytest.mark.parametrize("name", ["new-window", "new-session", "respawn-pane", "switch-client"])
def test_option_takes_c_on_the_commands_that_have_it(name: str) -> None:
    assert command(name).option("-c", "/d#x").build().argv == (name, "-c", "/d##x")


@pytest.mark.parametrize("value", ["-x", "--", "-t", "a#b", "n;"])
@pytest.mark.parametrize("name", ["new-window", "new-session"])
def test_window_name_takes_any_valid_name(name: str, value: str) -> None:
    # tmux takes the element after -n as its value even when it starts with "-" (spike E).
    expected = tb.escape_separator(tb.double_hashes(value))
    assert command(name).window_name(value).build().argv == (name, "-n", expected)


def test_window_name_is_only_for_new_window_and_new_session() -> None:
    for name in ("select-window", "rename-window", "set", "x"):
        with pytest.raises(ValueError):
            command(name).window_name("x")
    with pytest.raises(ValueError):
        command("new-window").window_name("#[fg=red]")


def test_session_name_takes_the_session_grammar() -> None:
    assert command("new-session").session_name("-w").build().argv == ("new-session", "-s", "-w")
    for value in ("", "a b", "a#b", "a;", "x" * 65):
        with pytest.raises(ValueError):
            command("new-session").session_name(value)
    with pytest.raises(ValueError):
        command("new-window").session_name("w")


def test_builder_has_one_entry_point() -> None:
    # R4-2: the builder's fields are not constructor arguments, and a bare builder is refused.
    with pytest.raises(TypeError):
        tb.CommandBuilder()
    with pytest.raises(TypeError):
        tb.CommandBuilder(_elements=["kill-pane", "-a"], _flags=["-t"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        dataclasses.replace(command("kill-pane"))


@pytest.mark.parametrize(
    "inner",
    [
        # R4-3: only the option names the spec sets inside a guard.
        _set_p("@a", "1"),
        _set_p("@termctl_other", "1"),
        _set_p("window-status-format", "#(x)"),
        _set_p("default-command", "x"),
        _set_p("automatic-rename", "off"),
        _set_w("@termctl_name", "x"),
        _set_w("remain-on-exit", "on"),
        _set_w("window-status-format", "#(x)"),
        command("set").flag("-p").target(tb.pane("%5")).build(),
        command("set").flag("-p").flag("-u").target(tb.pane("%5")).arg("status-left").build(),
    ],
    ids=lambda c: " ".join(c.elements),
)
def test_guard_sets_only_the_options_of_the_spec(inner: Command) -> None:
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), inner)


@pytest.mark.parametrize(
    "inner",
    [
        # R4-4 and R4-6: display inside a guard prints only ids and numbers, with -p.
        command("display").target(tb.pane("%5")).format_arg(tb.fields("pane_pid")).build(),
        command("display-message").target(tb.pane("%5")).format_arg(tb.fields("pane_pid")).build(),
        command("display").flag("-p").target(tb.pane("%5")).build(),
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(tb.fields("@termctl_name"))
        .build(),
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(tb.fields("window_name"))
        .build(),
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(tb.fields("pane_pid", "pane_title"))
        .build(),
        command("display").flag("-p").target(tb.pane("%5")).format_arg(tb.owned(ID)).build(),
    ],
    ids=lambda c: " ".join(c.elements),
)
def test_guarded_display_prints_only_termctl_controlled_text(inner: Command) -> None:
    with pytest.raises(ValueError):
        tb.guarded(tb.pane("%5"), tb.owned(ID), inner)


def test_guarded_display_takes_the_id_and_number_fields() -> None:
    names = ("pane_pid", "pane_dead", "pid", "pane_id", "window_id")
    for name in ("display", "display-message"):
        inner = command(name).flag("-p").target(tb.pane("%5")).format_arg(tb.fields(*names))
        assert tb.guarded(tb.pane("%5"), tb.owned(ID), inner.build()).guarded
