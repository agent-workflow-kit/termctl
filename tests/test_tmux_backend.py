"""Unit tests for the tmux argument builder, guarded writes, chains and the runner
(interface.md "tmux argument rules"). Real-tmux tests are in test_tmux_integration.py."""

from __future__ import annotations

import subprocess

import pytest

from termctl import tmux_backend as tb
from termctl.errors import TermctlError
from termctl.tmux_backend import Command, Format, Invocation, Tmux, command

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
        .option("-n", "n;")
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
        builder.option("-n", "x")
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
    for flag in ("-n", "-c", "-s"):
        cmd = command("new-session").option(flag, value).build()
        assert cmd.argv == ("new-session", flag, expected)
    assert command("rename-window").name(value).build().argv == ("rename-window", "--", expected)


@pytest.mark.parametrize("value", ["#[fg=red]", "a#[b", "##[", "#["])
def test_status_styles_are_refused_in_format_expanded_values(value: str) -> None:
    with pytest.raises(ValueError):
        command("new-window").option("-n", value)
    with pytest.raises(ValueError):
        command("rename-window").name(value)


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


def test_other_option_values_are_literal() -> None:
    assert command("switch-client").flag("-E").option("-c", "/dev/pts/1").build().argv == (
        "switch-client",
        "-E",
        "-c",
        "/dev/pts/1",
    )
    assert command("x").option("-x", "#{a}").build().argv == ("x", "-x", "#{a}")


def test_formats_are_passed_verbatim() -> None:
    fmt = Format("#{pane_id} #{window_id}")
    cmd = command("new-window").flag("-P").format("-F", fmt).build()
    assert cmd.argv == ("new-window", "-P", "-F", "#{pane_id} #{window_id}")
    assert command("display").flag("-p").format_arg(fmt).build().argv[-1] == fmt.text


@pytest.mark.parametrize("text", ["a;", "#{x}\n", "\x00", "x\x1b"])
def test_formats_reject_separators_and_control_characters(text: str) -> None:
    with pytest.raises(ValueError):
        Format(text)


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
    a = command("set-option").flag("-s").arg("exit-unattached").arg("off").build()
    b = command("new-session").flag("-d").option("-s", "work").build()
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
        tb.all_of(Format("1"))


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
    a = command("set-option").flag("-p").target(tb.pane("%5")).arg("@a").arg("1").build()
    b = (
        command("display")
        .flag("-p")
        .target(tb.pane("%5"))
        .format_arg(Format("#{pane_pid}"))
        .build()
    )
    cmd = tb.guarded(tb.pane("%5"), tb.owned(ID), a, b)
    assert cmd.argv[6] == (
        "'set-option' '-p' '-t' '%5' '--' '@a' '1' ; 'display' '-p' '-t' '%5' '--' '#{pane_pid}'"
    )


def test_guard_inner_rename_doubles_hashes_before_quoting() -> None:
    inner = command("rename-window").target(tb.window("@2")).name("a#(b)").build()
    cmd = tb.guarded(tb.pane("%5"), tb.owned_single_pane_window(ID), inner)
    assert cmd.argv[6] == "'rename-window' '-t' '@2' '--' 'a##(b)'"


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


@pytest.mark.parametrize("value", HOSTILE)
def test_quote_round_trips_like_a_posix_single_quote(value: str) -> None:
    # tmux's parser treats single quotes like the shell: nothing is special inside, and a
    # backslash outside quotes escapes the next character. The real-tmux round trip is in
    # test_tmux_integration.py; here, Python's shell lexer stands in.
    import shlex

    assert shlex.split(tb.quote(value)) == [value]


@pytest.mark.parametrize(
    ("stdout", "matched"),
    [
        ("", True),
        ("123 0 456\n", True),
        ("termctl-nomatch\n", False),
        ("termctl-nomatch", False),
        ("x\ntermctl-nomatch\n", False),
        ("termctl-nomatch-not\n", True),
        (" termctl-nomatch\n", True),
    ],
)
def test_guard_matched(stdout: str, matched: bool) -> None:
    assert tb.guard_matched(stdout) is matched


def test_check_guard_raises_not_found() -> None:
    with pytest.raises(TermctlError) as info:
        tb.check_guard("termctl-nomatch\n", "tc-abcdefgh")
    assert info.value.code == "not_found" and info.value.exit_code == 10
    tb.check_guard("", "tc-abcdefgh")


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


def test_command_is_immutable() -> None:
    cmd = command("kill-pane").target(tb.pane("%1")).build()
    assert isinstance(cmd, Command)
    with pytest.raises(AttributeError):
        cmd.argv = ()  # type: ignore[misc]
