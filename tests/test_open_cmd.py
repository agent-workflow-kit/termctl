"""``open``'s own steps up to the session (interface.md "Open order" steps 2 and 5, "Command",
"Server selection"), with the fake gateway. Real-tmux tests are in test_open_integration.py."""

from __future__ import annotations

import errno
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone

import pytest

from termctl import config, open_cmd, panes, tmux_socket
from termctl import tmux_backend as tb
from termctl.backend import Created, TmuxBackend
from termctl.errors import TermctlError
from termctl.gateway import FileInfo
from termctl.tmux_backend import Tmux, WindowSpec

from .fakes import UID, FakeGateway, completed, directory, safe_default_server, socket_file

TMUX = "/usr/bin/tmux"
SOCK = f"/tmp/tmux-{UID}/default"
ALLOW = ["PATH", "HOME", "LC_*"]

# --- the default name ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        ([], "shell"),
        (["vim"], "vim"),
        (["/usr/bin/python3", "-m", "x"], "python3"),
        (["./run.sh"], "run.sh"),
        (["dir/"], "command"),
        ([""], "command"),
        (["a\tb"], "command"),
        (["x#[fg=red]"], "command"),
        (["x" * 65], "command"),
        (["x" * 64], "x" * 64),
        (["PR#42"], "PR#42"),
        (["/bin/\udcff"], "command"),
    ],
)
def test_default_name(argv: list[str], name: str) -> None:
    assert open_cmd.default_name(argv) == name


# --- the PATH the command gets ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "environ", "allow", "path"),
    [
        ({}, {"PATH": "/usr/bin"}, ALLOW, "/usr/bin"),
        ({"PATH": "/opt/bin"}, {"PATH": "/usr/bin"}, ALLOW, "/opt/bin"),
        ({"PATH": ""}, {"PATH": "/usr/bin"}, ALLOW, ""),
        ({}, {"PATH": "/usr/bin"}, ["HOME"], None),
        ({}, {"PATH": "/usr/bin"}, ["PA*"], "/usr/bin"),
        ({}, {"PATH": "/usr/bin"}, ["*"], "/usr/bin"),
        ({}, {"PATH": "/usr/bin"}, ["PATHX"], None),
        ({}, {"PATH": "/usr/bin"}, ["P"], None),
        ({}, {}, ALLOW, None),
    ],
)
def test_command_path(
    env: dict[str, str], environ: dict[str, str], allow: list[str], path: str | None
) -> None:
    assert open_cmd.command_path(env, environ, allow) == path


@pytest.mark.parametrize(
    ("name", "allow", "kept"),
    [
        ("LC_ALL", ["LC_*"], True),
        ("LC_", ["LC_*"], True),
        ("LANG", ["LC_*"], False),
        ("XLC_ALL", ["LC_*"], False),
        ("PATH", ["PATH"], True),
        ("PATH2", ["PATH"], False),
        ("path", ["PATH"], False),
    ],
)
def test_allowed(name: str, allow: list[str], kept: bool) -> None:
    assert open_cmd.allowed(name, allow) is kept


# --- the command check --------------------------------------------------------------------------


def find(argv0: str, path: str | None, executables: set[str], cwd: str = "/work") -> FakeGateway:
    gateway = FakeGateway(executables=executables)
    open_cmd.check_command(argv0, cwd, path, gateway)
    return gateway


def not_found(argv0: str, path: str | None, executables: set[str], cwd: str = "/work") -> str:
    with pytest.raises(TermctlError) as caught:
        find(argv0, path, executables, cwd)
    assert (caught.value.code, caught.value.exit_code) == ("command_not_found", 12)
    return caught.value.message


def test_command_on_the_path_is_found_in_order() -> None:
    gateway = find("vim", "/a:/b:/c", {"/b/vim", "/c/vim"})
    assert gateway.executable_checks == ["/a/vim", "/b/vim"]


def test_command_missing_from_the_path() -> None:
    message = not_found("vim", "/a:/b", {"/c/vim"})
    assert "vim" in message


def test_relative_and_empty_path_entries_are_not_searched() -> None:
    # They would depend on the pane's directory; the conservative reading refuses them.
    gateway = FakeGateway(executables={"/work/vim", "/vim", "vim"})
    with pytest.raises(TermctlError):
        open_cmd.check_command("vim", "/work", ":.:bin::", gateway)
    assert gateway.executable_checks == []


def test_no_path_finds_no_bare_command() -> None:
    not_found("sh", None, {"/bin/sh", "/usr/bin/sh"})


def test_an_absolute_command_must_be_executable() -> None:
    assert find("/opt/tool", None, {"/opt/tool"}).executable_checks == ["/opt/tool"]
    not_found("/opt/tool", "/opt", set())


def test_a_relative_command_with_a_slash_is_resolved_against_the_cwd() -> None:
    assert find("./run.sh", "/usr/bin", {"/work/./run.sh"}).executable_checks == ["/work/./run.sh"]
    assert find("bin/x", None, {"/w d/bin/x"}, cwd="/w d").executable_checks == ["/w d/bin/x"]
    not_found("./run.sh", "/usr/bin", {"/usr/bin/run.sh", "./run.sh"})


@pytest.mark.parametrize("argv0", ["", ".", "..", "x\x00y"])
def test_degenerate_commands_are_not_found(argv0: str) -> None:
    not_found(argv0, "/usr/bin", set())


def test_the_message_escapes_nothing_itself_but_bounds_the_name() -> None:
    message = not_found("x" * 5000, "/usr/bin", set())
    assert len(message) < 400


# --- the size check -----------------------------------------------------------------------------


def spec(cwd: str = "/", name: str = "n") -> WindowSpec:
    return WindowSpec(name=name, cwd=cwd, command=open_cmd.PLACEHOLDER)


def size(invocation: tb.Invocation) -> int:
    argv = invocation.argv(TMUX, SOCK)
    return sum(len(e.encode("utf-8", "surrogateescape")) for e in argv)


def test_size_check_passes_at_the_limit() -> None:
    base = size(tb.new_session("work", spec("/"), start_server=True, env={}))
    cwd = "/" + "d" * (open_cmd.SIZE_LIMIT - base)
    invocation = tb.new_session("work", spec(cwd), start_server=True, env={})
    assert size(invocation) == open_cmd.SIZE_LIMIT
    open_cmd.check_size([invocation], TMUX, SOCK)


def test_size_check_fails_one_byte_over() -> None:
    base = size(tb.new_session("work", spec("/"), start_server=True, env={}))
    cwd = "/" + "d" * (open_cmd.SIZE_LIMIT - base + 1)
    with pytest.raises(TermctlError) as caught:
        invocation = tb.new_session("work", spec(cwd), start_server=True, env={})
        open_cmd.check_size([invocation], TMUX, SOCK)
    assert (caught.value.code, caught.value.exit_code) == ("usage", 2)
    assert "12288" in caught.value.message


def test_size_check_counts_utf8_bytes_and_escapes() -> None:
    # 4 bytes per character, and the final ';' escape adds one: counted as tmux gets them.
    base = size(tb.new_session("work", spec("/"), start_server=True, env={}))
    room = open_cmd.SIZE_LIMIT - base
    cwd = "/" + "\U0001f514" * (room // 4)
    invocation = tb.new_session("work", spec(cwd), start_server=True, env={})
    open_cmd.check_size([invocation], TMUX, SOCK)
    over = tb.new_session("work", spec(cwd + "\U0001f514"), start_server=True, env={})
    with pytest.raises(TermctlError):
        open_cmd.check_size([over], TMUX, SOCK)


def test_size_check_judges_the_largest_invocation() -> None:
    small = tb.has_session("work")
    big = tb.new_window("work", spec("/" + "d" * 13000))
    with pytest.raises(TermctlError):
        open_cmd.check_size([small, big], TMUX, SOCK)


def plan(
    argv: tuple[str, ...] = ("vim",),
    env: tuple[tuple[str, str], ...] = (),
    *,
    cwd: str = "/w",
    keep: str = "never",
    labels: tuple[str, ...] = ("a=b", "k=v"),
) -> open_cmd.Plan:
    return open_cmd.Plan(
        name="n",
        cwd=cwd,
        argv=argv,
        env=env,
        labels=labels,
        placement="default",
        keep_on_exit=keep,
    )


def test_with_argv_the_window_runs_the_placeholder_without_env() -> None:
    # Open order step 6: the placeholder gets no -e variables; the real command does.
    assert plan(env=(("A", "1"),)).window == WindowSpec("n", "/w", open_cmd.PLACEHOLDER)
    # Review round 1, R1-3: sleep is looked up on a fixed PATH, never the caller's.
    assert open_cmd.PLACEHOLDER == ("/usr/bin/env", "--", "PATH=/usr/bin:/bin", "sleep", "30")


def test_without_argv_the_window_runs_the_default_shell_with_env() -> None:
    # Spike E2: no command, so tmux starts its default shell as a login shell; the --env
    # variables go to new-window or new-session as -e (spike G3c).
    assert plan((), (("A", "1"),)).window == WindowSpec("n", "/w", None, (("A", "1"),))


def test_respawn_carries_argv_and_env_but_path_through_usr_bin_env() -> None:
    respawn = plan(("tool", "-x"), (("A", "1"), ("PATH", "/opt/bin"), ("B", ""))).respawn
    assert respawn == tb.Respawn(
        cwd="/w", argv=("tool", "-x"), env=(("A", "1"), ("B", "")), path="/opt/bin"
    )


def test_no_respawn_without_argv() -> None:
    assert plan(()).respawn is None


def test_options_are_every_pane_option_with_the_ownership_marker_last() -> None:
    options = plan(labels=("a=b", "k=v")).options(
        "tc-abcdefgh", Created("%5", "@3"), "2026-10-30T00:00:00.000Z"
    )
    assert options == [
        ("@termctl_v", "1"),
        ("@termctl_window", "@3"),
        ("@termctl_id", "tc-abcdefgh"),
        ("@termctl_name", "n"),
        ("@termctl_marker", ""),
        ("@termctl_marker_text", ""),
        ("@termctl_labels", "a=b k=v"),
        ("@termctl_placement", "default"),
        ("@termctl_keep", "never"),
        ("@termctl_created", "2026-10-30T00:00:00.000Z"),
        ("@termctl_pid", ""),
        ("@termctl_pid_start", ""),
        ("@termctl_pane", "%5"),
    ]
    # Every option the pane reader reads, and nothing else.
    assert sorted(name for name, _ in options) == sorted(panes.OPTION_FIELDS)


@pytest.mark.parametrize(
    ("keep", "remain"), [("never", "off"), ("failed", "failed"), ("always", "on")]
)
def test_keep_on_exit_becomes_remain_on_exit(keep: str, remain: str) -> None:
    created = Created("%5", "@3")
    argv = plan(keep=keep).tag_invocation(ID, created, "2026-10-30T00:00:00.000Z").argv(TMUX, SOCK)
    assert f"'remain-on-exit' '{remain}'" in argv[-2]
    assert f"'@termctl_keep' '{keep}'" in argv[-2]


@pytest.mark.parametrize(
    ("now", "text"),
    [
        (datetime(2026, 10, 30, 12, 0, 0, 0, tzinfo=UTC), "2026-10-30T12:00:00.000Z"),
        (datetime(2026, 10, 30, 12, 0, 0, 999999, tzinfo=UTC), "2026-10-30T12:00:00.999Z"),
        (
            datetime(2026, 10, 30, 14, 0, 0, 1000, tzinfo=timezone(timedelta(hours=2))),
            "2026-10-30T12:00:00.001Z",
        ),
    ],
)
def test_timestamp_is_utc_with_milliseconds(now: datetime, text: str) -> None:
    assert open_cmd.timestamp(now) == text


def test_size_check_counts_the_tagging_invocation() -> None:
    # A long argv fits the new-session invocation (it gets the placeholder) but not step 6's.
    big = plan(("x" * 13000,))
    invocations = open_cmd.size_invocations("work", big, ID, {})
    assert size(invocations[0]) < open_cmd.SIZE_LIMIT
    with pytest.raises(TermctlError) as caught:
        open_cmd.check_size(invocations, TMUX, SOCK)
    assert (caught.value.code, caught.value.exit_code) == ("usage", 2)


def test_size_check_counts_env_values_once_quoted() -> None:
    # Inside the guard each byte 0xff is written \377 outside the quotes, so a run of them
    # costs six bytes each (''\377), not one: the check counts what tmux gets.
    invocations = open_cmd.size_invocations("work", plan(), ID, {})
    room = open_cmd.SIZE_LIMIT - max(size(i) for i in invocations)
    fits = plan(env=(("A", "\udcff" * ((room - 20) // 6)),))
    open_cmd.check_size(open_cmd.size_invocations("work", fits, ID, {}), TMUX, SOCK)
    over = plan(env=(("A", "\udcff" * (room // 6 + 1)),))
    with pytest.raises(TermctlError):
        open_cmd.check_size(open_cmd.size_invocations("work", over, ID, {}), TMUX, SOCK)


def test_size_check_without_argv_counts_env_on_new_session() -> None:
    no_argv = plan((), (("A", "v" * 13000),))
    new_session = open_cmd.size_invocations("work", no_argv, ID, {})[0]
    assert size(new_session) > open_cmd.SIZE_LIMIT


def test_size_check_counts_the_filtered_values_set_back_on_new_session() -> None:
    # Review round 1, R1-2: the new-session chain carries the filtered value of each --env
    # variable the filtered environment has.
    no_argv = plan((), (("LANG", "x"),))
    filtered = {"LANG": "v" * 13000}
    new_session = open_cmd.size_invocations("work", no_argv, ID, filtered)[0]
    assert size(new_session) > open_cmd.SIZE_LIMIT
    assert size(open_cmd.size_invocations("work", no_argv, ID, {})[0]) < open_cmd.SIZE_LIMIT


def test_size_check_assumes_the_largest_ids() -> None:
    tagging = open_cmd.size_invocations("work", plan(), ID, {})[1]
    assert "%4294967295" in tagging.argv(TMUX, SOCK)


# --- step 1: argv ---------------------------------------------------------------------------------


@pytest.mark.parametrize("argv0", ["a=b", "=", "=x", "x=", "/usr/bin/a=b", "PATH=/x"])
def test_argv0_with_an_equals_sign_is_a_usage_error(argv0: str) -> None:
    with pytest.raises(TermctlError) as caught:
        open_cmd.check_argv([argv0, "y"], {})
    assert (caught.value.code, caught.value.exit_code) == ("usage", 2)


def test_equals_signs_after_argv0_are_fine() -> None:
    open_cmd.check_argv(["env", "A=b", "="], {})


def test_env_path_without_argv_is_a_usage_error() -> None:
    with pytest.raises(TermctlError) as caught:
        open_cmd.check_argv([], {"PATH": "/opt/secret-dir-value"})
    assert (caught.value.code, caught.value.exit_code) == ("usage", 2)
    assert "secret-dir-value" not in caught.value.message


def test_env_path_with_argv_and_other_env_without_argv_are_fine() -> None:
    open_cmd.check_argv(["x"], {"PATH": "/opt"})
    open_cmd.check_argv([], {"PATHX": "/opt", "A": "1"})


# --- the filtered environment ---------------------------------------------------------------------


def test_filtered_env_keeps_only_allowed_names() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "SSH_AUTH_SOCK": "/s",
        "AWS_SECRET_ACCESS_KEY": "k",
        "TMUX": "/tmp/tmux-1000/default,1,0",
    }
    assert open_cmd.filtered_env(environ, ALLOW) == {
        "PATH": "/usr/bin",
        "HOME": "/h",
        "LC_ALL": "C.UTF-8",
    }


def test_filtered_env_with_the_default_allowlist_keeps_a_utf8_locale() -> None:
    # #10: tmux checks the locale before anything else; the default keeps LANG and LC_*.
    allow = config.defaults().server_env_allow
    environ = {"LANG": "de_DE.UTF-8", "LC_CTYPE": "de_DE.UTF-8", "SECRET": "x"}
    assert open_cmd.filtered_env(environ, allow) == {
        "LANG": "de_DE.UTF-8",
        "LC_CTYPE": "de_DE.UTF-8",
    }


def test_filtered_env_with_an_empty_allowlist_is_empty() -> None:
    assert open_cmd.filtered_env({"PATH": "/usr/bin"}, []) == {}


# --- step 6: tagging and cleanup ------------------------------------------------------------------

ID = "tc-abcdefgh"
AT = "2026-10-30T00:00:00.000Z"
PANE = Created("%5", "@3")


def tag_key(p: open_cmd.Plan) -> tuple[str, ...]:
    return tuple(p.tag_invocation(ID, PANE, AT).argv(TMUX, SOCK))


CLEANUP = tuple(tb.cleanup_kill("%5", "@3").argv(TMUX, SOCK))
NOMATCH = completed("termctl-nomatch\n")
# The line the tagging invocation ends with when it ran to the end.
TAGGED = f"{tb.TAG_CONFIRMATION}\n"


def tag(gateway: FakeGateway, p: open_cmd.Plan, warnings: list[str] | None = None) -> None:
    backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env={}))
    open_cmd.tag(backend, PANE, ID, p, AT, (warnings if warnings is not None else []).append)


def test_tag_runs_one_guarded_invocation() -> None:
    p = plan()
    gateway = FakeGateway({tag_key(p): completed(TAGGED)})
    tag(gateway, p)
    assert gateway.calls == [list(tag_key(p))]


@pytest.mark.parametrize(
    "stdout",
    ["", "4242 0 77\n", "4242 0 77\ntermctl-tagged-not\n", TAGGED + "4242 0 77\n"],
    ids=["empty", "query-only", "other-line", "not-last"],
)
def test_a_tagging_that_exits_0_unconfirmed_is_tag_failed_and_cleaned_up(stdout: str) -> None:
    # Review round 2, R2-1: a tmux client killed mid-chain can exit 0 with the rest of the
    # chain dropped; only the confirmation the chain ends with counts as tagged.
    p = plan()
    gateway = FakeGateway({tag_key(p): completed(stdout), CLEANUP: completed()})
    warnings: list[str] = []
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p, warnings)
    assert (caught.value.code, caught.value.exit_code) == ("tag_failed", 12)
    assert gateway.calls == [list(tag_key(p)), list(CLEANUP)]
    assert warnings == []


@pytest.mark.parametrize(
    "result",
    [
        NOMATCH,
        completed(stderr="can't find pane: %5\n", returncode=1),
        completed(stderr="no server running on x\n", returncode=1),
        subprocess.TimeoutExpired(["tmux"], 30),
        OSError("gone"),
    ],
    ids=["nomatch", "failed", "other-socket-no-server", "timeout", "oserror"],
)
def test_a_failed_tagging_kills_the_pane_and_exits_tag_failed(result: object) -> None:
    p = plan()
    gateway = FakeGateway({tag_key(p): result, CLEANUP: completed()})  # type: ignore[dict-item]
    warnings: list[str] = []
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p, warnings)
    assert (caught.value.code, caught.value.exit_code) == ("tag_failed", 12)
    assert gateway.calls[-1] == list(CLEANUP)
    assert warnings == []


@pytest.mark.parametrize(
    "stderr",
    [f"no server running on {SOCK}\n", f"error connecting to {SOCK} (No such file or directory)\n"],
)
def test_a_tagging_that_found_no_server_connects_no_more(stderr: str) -> None:
    # Review round 1, R1-6: a no-server result is never followed by a connect without the
    # socket checks, so there is no cleanup kill (and nothing to kill).
    p = plan()
    gateway = FakeGateway({tag_key(p): completed(stderr=stderr, returncode=1)})
    warnings: list[str] = []
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p, warnings)
    assert (caught.value.code, caught.value.exit_code) == ("tag_failed", 12)
    assert caught.value.details is None
    assert gateway.calls == [list(tag_key(p))]
    assert warnings == []


@pytest.mark.parametrize(
    "cleanup",
    [NOMATCH, completed(stderr="boom\n", returncode=1), subprocess.TimeoutExpired(["t"], 30)],
    ids=["nomatch", "failed", "timeout"],
)
def test_a_cleanup_that_kills_nothing_warns_and_still_reports_tag_failed(cleanup: object) -> None:
    # Review round 1, R1-7: a forged @termctl_pane, say, leaves an unmanaged window behind.
    p = plan()
    gateway = FakeGateway({tag_key(p): NOMATCH, CLEANUP: cleanup})  # type: ignore[dict-item]
    warnings: list[str] = []
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p, warnings)
    assert caught.value.code == "tag_failed"
    assert len(warnings) == 1
    assert "%5" in warnings[0] and "@3" in warnings[0]


@pytest.mark.parametrize(
    "stderr", ["can't find pane: %5\n", "can't find window: @3\n"], ids=["pane", "window"]
)
def test_a_cleanup_that_finds_the_pane_gone_does_not_warn(stderr: str) -> None:
    # Review round 2, R2-2: the pane is already gone (a no-argv shell that exited at once,
    # another client's kill), so nothing is left running.
    p = plan()
    gateway = FakeGateway({tag_key(p): NOMATCH, CLEANUP: completed(stderr=stderr, returncode=1)})
    warnings: list[str] = []
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p, warnings)
    assert caught.value.code == "tag_failed"
    assert gateway.calls[-1] == list(CLEANUP)
    assert warnings == []


def test_a_cleanup_whose_failure_only_starts_like_a_gone_message_warns() -> None:
    p = plan()
    stderr = "boom\ncan't find pane: %5\n"
    gateway = FakeGateway({tag_key(p): NOMATCH, CLEANUP: completed(stderr=stderr, returncode=1)})
    warnings: list[str] = []
    with pytest.raises(TermctlError):
        tag(gateway, p, warnings)
    assert len(warnings) == 1


def test_tag_failed_never_echoes_env_values() -> None:
    p = plan(env=(("TOKENISH", "value-not-to-print"),))
    gateway = FakeGateway({tag_key(p): completed(returncode=1), CLEANUP: completed()})
    with pytest.raises(TermctlError) as caught:
        tag(gateway, p)
    assert "value-not-to-print" not in caught.value.message


def test_tag_returns_the_answer_of_the_step_7_query() -> None:
    p = plan()
    gateway = FakeGateway({tag_key(p): completed("4242 0 77\n" + TAGGED)})
    backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env={}))
    assert open_cmd.tag(backend, PANE, ID, p, AT, [].append) == tb.ProcessQuery(4242, False, 77)


@pytest.mark.parametrize("stdout", ["", "  \n", "garbage\n", "4242 0\n"])
def test_tag_returns_none_when_the_query_failed(stdout: str) -> None:
    p = plan()
    gateway = FakeGateway({tag_key(p): completed(stdout + TAGGED)})
    backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env={}))
    assert open_cmd.tag(backend, PANE, ID, p, AT, [].append) is None
    assert gateway.calls == [list(tag_key(p))]


# --- step 7: the process --------------------------------------------------------------------------

RECORD = tuple(tb.record_process("%5", ID, "4242", "823520334").argv(TMUX, SOCK))
TAIL = "0 -1 4194304 131 0 0 0 0 0 0 0 20 0 1 0 823520334 3436544 70 18446744073709551615 0"


def proc_stat(state: str = "S", ppid: int = 77) -> str:
    return f"4242 (tool) {state} {ppid} 4242 4242 {TAIL}\n"


def record(
    gateway: FakeGateway, query: tb.ProcessQuery | None, warnings: list[str] | None = None
) -> bool:
    backend = TmuxBackend(Tmux(gateway, TMUX, SOCK, env={}))
    return open_cmd.record(
        backend, gateway, SOCK, PANE, ID, query, (warnings if warnings is not None else []).append
    )


RUNNING = tb.ProcessQuery(4242, False, 77)
# The server termctl reaches through the socket is the queried server (SO_PEERCRED).
SERVER_PEER = {SOCK: 77}


def test_a_running_child_of_the_server_is_recorded() -> None:
    # Spike E6: not dead, and its parent is the server, so the PID is the pane's process.
    gateway = FakeGateway(
        {RECORD: completed()}, proc_stats={4242: proc_stat()}, peer_pids=SERVER_PEER
    )
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is True
    assert gateway.peer_checks == [SOCK]
    assert gateway.proc_reads == [4242]
    assert gateway.calls == [list(RECORD)]
    assert warnings == []


@pytest.mark.parametrize(
    ("query", "stat"),
    [
        (None, None),
        (tb.ProcessQuery(4242, True, 77), proc_stat()),
        (RUNNING, proc_stat(ppid=78)),
        (RUNNING, proc_stat(ppid=1)),
        (RUNNING, proc_stat(state="Z")),
        (RUNNING, proc_stat(state="X")),
        (RUNNING, None),
    ],
    ids=["query-failed", "dead", "other-parent", "reparented", "zombie", "reaped", "gone"],
)
def test_nothing_is_recorded_unless_provably_the_servers_live_child(
    query: tb.ProcessQuery | None, stat: str | None
) -> None:
    gateway = FakeGateway(proc_stats={4242: stat}, peer_pids=SERVER_PEER)
    warnings: list[str] = []
    assert record(gateway, query, warnings) is False
    assert gateway.calls == []
    assert warnings == []
    if query is None or query.dead:
        assert gateway.proc_reads == []


@pytest.mark.parametrize(
    "stat", [PermissionError(13, "Permission denied"), "4242 (tool) S x\n"], ids=["eacces", "bad"]
)
def test_a_process_that_cannot_be_read_warns_and_records_nothing(stat: object) -> None:
    gateway = FakeGateway(proc_stats={4242: stat}, peer_pids=SERVER_PEER)  # type: ignore[dict-item]
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert gateway.calls == []
    assert len(warnings) == 1 and "%5" in warnings[0]


@pytest.mark.parametrize(
    ("own", "problem"),
    [
        (False, "/proc belongs to another PID namespace than termctl"),
        (PermissionError(13, "Permission denied"), "cannot read /proc/self: Permission denied"),
    ],
)
def test_a_proc_of_another_pid_namespace_warns_and_records_nothing(
    own: bool | OSError, problem: str
) -> None:
    gateway = FakeGateway(proc_stats={4242: proc_stat()}, peer_pids=SERVER_PEER)
    gateway.own_namespace = own
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert gateway.calls == [] and gateway.proc_reads == []
    assert len(warnings) == 1 and problem in warnings[0] and "%5" in warnings[0]


def test_a_recording_whose_guard_no_longer_matches_is_silent() -> None:
    # The pane died, closed or changed between the query and the recording.
    gateway = FakeGateway({RECORD: NOMATCH}, proc_stats={4242: proc_stat()}, peer_pids=SERVER_PEER)
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert warnings == []


@pytest.mark.parametrize(
    "result",
    [completed(stderr="boom\n", returncode=1), subprocess.TimeoutExpired(["tmux"], 30)],
    ids=["failed", "timeout"],
)
def test_a_failed_recording_warns_and_never_raises(result: object) -> None:
    gateway = FakeGateway(
        {RECORD: result},  # type: ignore[dict-item]
        proc_stats={4242: proc_stat()},
        peer_pids=SERVER_PEER,
    )
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert len(warnings) == 1 and "%5" in warnings[0]


def test_macos_reads_the_parent_with_ps() -> None:
    ps = ("/bin/ps", "-o", "ppid=", "-o", "stat=", "-o", "lstart=", "-p", "4242")
    start = "1793402720"  # 2026-10-30T23:25:20Z
    recorded = tuple(tb.record_process("%5", ID, "4242", start).argv(TMUX, SOCK))
    gateway = FakeGateway(
        {ps: completed("77 S Fri Oct 30 23:25:20 2026\n"), recorded: completed()},
        system="darwin",
    )
    assert record(gateway, RUNNING) is True
    assert gateway.peer_checks == []  # macOS has no PID namespaces
    assert gateway.calls == [list(ps), list(recorded)]


# Review round 1 of #16, R1-8: #{pid} and #{pane_pid} are in the server's PID namespace, /proc
# in termctl's; the server reached through the socket must be the queried one.
@pytest.mark.parametrize("peer", [78, 0], ids=["other-server", "not-visible"])
def test_a_server_in_another_pid_namespace_records_nothing(peer: int) -> None:
    gateway = FakeGateway(proc_stats={4242: proc_stat()}, peer_pids={SOCK: peer})
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert gateway.proc_reads == []
    assert gateway.calls == []
    assert warnings == []


def test_a_server_check_that_cannot_be_made_warns_and_records_nothing() -> None:
    refused = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    gateway = FakeGateway(proc_stats={4242: proc_stat()}, peer_pids={SOCK: refused})
    warnings: list[str] = []
    assert record(gateway, RUNNING, warnings) is False
    assert gateway.proc_reads == []
    assert gateway.calls == []
    assert len(warnings) == 1 and "%5" in warnings[0]


# --- step 5: server and session -----------------------------------------------------------------

SPEC = spec("/w", "KEY-12")


def key(invocation: tb.Invocation) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, SOCK))


HAS = key(tb.has_session("work"))
START = key(tb.new_session("work", SPEC, start_server=True, env={}))
NEW_SESSION = key(tb.new_session("work", SPEC, start_server=False, env={}))
NEW_WINDOW = key(tb.new_window("work", SPEC))

NO_SERVER = completed(stderr=f"no server running on {SOCK}\n", returncode=1)
MISSING = completed(stderr="can't find session: work\n", returncode=1)
DUPLICATE = completed(stderr="duplicate session: work\n", returncode=1)
OK = completed()


Effect = Callable[[FakeGateway], None]


def socket_appears(gateway: FakeGateway) -> None:
    """A server starts and creates its socket file."""
    gateway.files[SOCK] = socket_file()


def socket_gone(gateway: FakeGateway) -> None:
    """The server exits and removes its socket file."""
    del gateway.files[SOCK]


def directory_unsafe(gateway: FakeGateway) -> None:
    """The socket's directory becomes writable by others."""
    gateway.files[f"/tmp/tmux-{UID}"] = directory(0o777, UID)


class Script(FakeGateway):
    """A fake gateway whose tmux answers come from a per-argv queue, so a test can script the
    same invocation answering differently each time (a race). ``after`` runs an effect on
    the gateway after each answer of an argv, one per call in order (another process acting
    right after that call)."""

    def __init__(
        self,
        script: Mapping[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
        after: Mapping[tuple[str, ...], list[Effect | None]] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.script = {k: list(v) for k, v in script.items()}
        self.after = {k: list(v) for k, v in (after or {}).items()}

    def run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        queue = self.script.get(tuple(argv))
        if not queue:
            raise AssertionError(f"unexpected gateway call: {argv!r}")
        effects = self.after.get(tuple(argv))
        effect = effects.pop(0) if effects else None
        if effect is not None:
            effect(self)
        return queue.pop(0)

    def tmux_calls(self) -> list[tuple[str, ...]]:
        return [tuple(call) for call in self.calls]


def create(gateway: FakeGateway, socket: str | None = None) -> Created:
    server = tmux_socket.resolve(socket, None, {}, gateway)
    backend = TmuxBackend(Tmux(gateway, TMUX, server.socket_path, env={}))
    return open_cmd.create_window(backend, server, gateway, "work", SPEC)


def create_error(gateway: FakeGateway, socket: str | None = None) -> TermctlError:
    with pytest.raises(TermctlError) as caught:
        create(gateway, socket)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    return caught.value


def no_socket() -> dict[str, FileInfo]:
    """A filesystem with a safe default directory and no socket file."""
    files = safe_default_server()
    del files[SOCK]
    return files


def test_existing_session_gets_a_new_window() -> None:
    gateway = Script({HAS: [OK], NEW_WINDOW: [completed("%5 @3\n")]})
    assert create(gateway) == Created("%5", "@3")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW]


def test_missing_session_is_created_without_the_server_prefix_and_confirmed() -> None:
    # Spike F2: a running server's settings are never changed.
    gateway = Script({HAS: [MISSING, OK], NEW_SESSION: [completed("%5 @3\n")]})
    assert create(gateway) == Created("%5", "@3")
    assert gateway.tmux_calls() == [HAS, NEW_SESSION, HAS]
    assert "exit-unattached" not in NEW_SESSION


def test_no_server_starts_one_with_the_prefix_and_confirms() -> None:
    gateway = Script(
        {START: [completed("%0 @0\n")], HAS: [OK]}, {START: [socket_appears]}, files=no_socket()
    )
    assert create(gateway) == Created("%0", "@0")
    # No socket file: no probe before the start, has-session after it.
    assert gateway.tmux_calls() == [START, HAS]
    assert START[3:8] == ("set", "-s", "--", "exit-unattached", "off")


def test_a_stale_socket_file_is_no_server() -> None:
    # The socket file is still there when the server starts: probed again, then started.
    gateway = Script({HAS: [NO_SERVER, NO_SERVER, OK], START: [completed("%0 @0\n")]})
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, HAS, START, HAS]


def test_a_started_server_that_is_not_there_is_retried_once_then_server_unavailable() -> None:
    gateway = Script(
        {START: [completed("%0 @0\n")] * 2, HAS: [NO_SERVER] * 4},
        {START: [socket_appears]},
        files=no_socket(),
    )
    assert "work" in create_error(gateway).message
    assert gateway.tmux_calls() == [START, HAS, HAS, HAS, START, HAS]


def test_a_started_server_without_the_session_is_retried_by_the_path_the_probe_selects() -> None:
    gateway = Script(
        {
            START: [completed("%0 @0\n")],
            HAS: [MISSING, MISSING, OK],
            NEW_SESSION: [completed("%1 @1\n")],
        },
        {START: [socket_appears]},
        files=no_socket(),
    )
    assert create(gateway) == Created("%1", "@1")
    assert gateway.tmux_calls() == [START, HAS, HAS, NEW_SESSION, HAS]


def test_no_server_creates_the_default_directory_first() -> None:
    files = safe_default_server()
    del files[SOCK], files[f"/tmp/tmux-{UID}"]
    gateway = Script(
        {START: [completed("%0 @0\n")], HAS: [OK]}, {START: [socket_appears]}, files=files
    )
    assert create(gateway) == Created("%0", "@0")
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]


def test_explicit_path_with_a_missing_parent_never_starts_a_server() -> None:
    gateway = Script({})
    err = create_error(gateway, "/srv/missing/sock")
    assert "does not exist" in err.message
    assert gateway.calls == [] and gateway.mkdirs == []


def test_an_unsafe_socket_is_never_connected() -> None:
    files = safe_default_server()
    files[f"/tmp/tmux-{UID}"] = directory(0o777, UID)
    gateway = Script({}, files=files)
    create_error(gateway)
    assert gateway.calls == []


def test_a_socket_made_unsafe_before_the_start_is_never_connected() -> None:
    # The default directory appears between the probe and the start, unsafe.
    files = safe_default_server()
    del files[SOCK], files[f"/tmp/tmux-{UID}"]
    gateway = Script({}, files=files)
    gateway.appears[f"/tmp/tmux-{UID}"] = directory(0o777, UID)
    create_error(gateway)
    assert gateway.calls == []


def test_a_socket_made_unsafe_after_the_probe_is_never_connected() -> None:
    gateway = Script({HAS: [OK]}, {HAS: [directory_unsafe]})
    create_error(gateway)
    assert gateway.tmux_calls() == [HAS]


def test_a_socket_made_unsafe_after_new_session_is_never_connected() -> None:
    gateway = Script(
        {HAS: [MISSING], NEW_SESSION: [completed("%5 @3\n")]}, {NEW_SESSION: [directory_unsafe]}
    )
    create_error(gateway)
    assert gateway.tmux_calls() == [HAS, NEW_SESSION]


def test_a_socket_gone_after_the_probe_is_no_server() -> None:
    # No connect after the checks found no socket: the server is started instead.
    gateway = Script(
        {HAS: [OK, OK], START: [completed("%0 @0\n")]},
        {HAS: [socket_gone], START: [socket_appears]},
    )
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, START, HAS]


def test_duplicate_session_race_falls_back_to_a_new_window() -> None:
    gateway = Script({HAS: [MISSING], NEW_SESSION: [DUPLICATE], NEW_WINDOW: [completed("%9 @9\n")]})
    assert create(gateway) == Created("%9", "@9")
    assert gateway.tmux_calls() == [HAS, NEW_SESSION, NEW_WINDOW]


def test_server_started_by_another_caller_falls_back_to_a_new_window() -> None:
    # The other caller's server has the session: the prefix ran there (a residual risk), and
    # termctl takes the new-window path without confirming a server it did not start.
    gateway = Script(
        {START: [DUPLICATE], NEW_WINDOW: [completed("%9 @9\n")]},
        {START: [socket_appears]},
        files=no_socket(),
    )
    assert create(gateway) == Created("%9", "@9")
    assert gateway.tmux_calls() == [START, NEW_WINDOW]


@pytest.mark.parametrize(
    ("state", "path"), [(OK, NEW_WINDOW), (MISSING, NEW_SESSION)], ids=["exists", "missing"]
)
def test_a_server_started_after_the_probe_never_gets_the_prefix(
    state: subprocess.CompletedProcess[str], path: tuple[str, ...]
) -> None:
    # Another caller's server creates its socket between the probe and the start.
    gateway = Script({HAS: [state, OK], path: [completed("%9 @9\n")]}, files=no_socket())
    gateway.appears[SOCK] = socket_file()
    assert create(gateway) == Created("%9", "@9")
    assert START not in gateway.tmux_calls()
    assert gateway.tmux_calls()[:2] == [HAS, path]


def test_vanished_session_is_retried_once_by_the_path_the_probe_selects() -> None:
    gateway = Script(
        {HAS: [OK, MISSING, OK], NEW_WINDOW: [MISSING], NEW_SESSION: [completed("%2 @2\n")]}
    )
    assert create(gateway) == Created("%2", "@2")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, NEW_SESSION, HAS]


def test_vanished_session_and_server_restarts_the_server() -> None:
    gateway = Script(
        {HAS: [OK, OK], NEW_WINDOW: [MISSING], START: [completed("%0 @0\n")]},
        {NEW_WINDOW: [socket_gone], START: [socket_appears]},
    )
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, START, HAS]


def test_session_vanishing_twice_is_server_unavailable_never_10() -> None:
    gateway = Script(
        {
            HAS: [OK, MISSING],
            NEW_WINDOW: [MISSING, MISSING],
            NEW_SESSION: [DUPLICATE],
        }
    )
    err = create_error(gateway)
    assert "work" in err.message
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, NEW_SESSION, NEW_WINDOW]


def test_session_vanishing_again_after_the_retry_is_server_unavailable() -> None:
    gateway = Script({HAS: [OK, OK], NEW_WINDOW: [MISSING, MISSING]})
    create_error(gateway)
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, NEW_WINDOW]


def test_server_vanishing_at_new_window_is_retried_by_starting_it() -> None:
    # The last session vanished and took the server with it (exit-empty).
    gateway = Script(
        {HAS: [OK, OK], NEW_WINDOW: [NO_SERVER], START: [completed("%0 @0\n")]},
        {NEW_WINDOW: [socket_gone], START: [socket_appears]},
    )
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, START, HAS]


def test_server_vanishing_at_new_window_twice_is_server_unavailable() -> None:
    gateway = Script({HAS: [OK, OK], NEW_WINDOW: [NO_SERVER, NO_SERVER]})
    create_error(gateway)
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, NEW_WINDOW]


def test_new_session_that_started_a_server_without_the_prefix_is_retried() -> None:
    # The server ends between the probe and new-session; new-session starts one without the
    # prefix, which a user's exit-unattached on ends at once: the confirmation finds no
    # server, and the retry starts it with the prefix.
    gateway = Script(
        {HAS: [MISSING, OK], NEW_SESSION: [completed("%0 @0\n")], START: [completed("%1 @1\n")]},
        {NEW_SESSION: [socket_gone], START: [socket_appears]},
    )
    assert create(gateway) == Created("%1", "@1")
    assert gateway.tmux_calls() == [HAS, NEW_SESSION, START, HAS]


def test_probe_failure_is_server_unavailable() -> None:
    gateway = Script(
        {HAS: [completed(stderr=f"error connecting to {SOCK} (Permission denied)\n", returncode=1)]}
    )
    create_error(gateway)
