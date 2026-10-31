"""``open``'s own steps up to the session (interface.md "Open order" steps 2 and 5, "Command",
"Server selection"), with the fake gateway. Real-tmux tests are in test_open_integration.py."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest

from termctl import open_cmd, tmux_socket
from termctl import tmux_backend as tb
from termctl.backend import Created, TmuxBackend
from termctl.errors import TermctlError
from termctl.tmux_backend import Tmux, WindowSpec

from .fakes import UID, FakeGateway, completed, directory, safe_default_server

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
    base = size(tb.new_session("work", spec("/"), start_server=True))
    cwd = "/" + "d" * (open_cmd.SIZE_LIMIT - base)
    invocation = tb.new_session("work", spec(cwd), start_server=True)
    assert size(invocation) == open_cmd.SIZE_LIMIT
    open_cmd.check_size([invocation], TMUX, SOCK)


def test_size_check_fails_one_byte_over() -> None:
    base = size(tb.new_session("work", spec("/"), start_server=True))
    cwd = "/" + "d" * (open_cmd.SIZE_LIMIT - base + 1)
    with pytest.raises(TermctlError) as caught:
        open_cmd.check_size([tb.new_session("work", spec(cwd), start_server=True)], TMUX, SOCK)
    assert (caught.value.code, caught.value.exit_code) == ("usage", 2)
    assert "12288" in caught.value.message


def test_size_check_counts_utf8_bytes_and_escapes() -> None:
    # 4 bytes per character, and the final ';' escape adds one: counted as tmux gets them.
    base = size(tb.new_session("work", spec("/"), start_server=True))
    room = open_cmd.SIZE_LIMIT - base
    cwd = "/" + "\U0001f514" * (room // 4)
    invocation = tb.new_session("work", spec(cwd), start_server=True)
    open_cmd.check_size([invocation], TMUX, SOCK)
    over = tb.new_session("work", spec(cwd + "\U0001f514"), start_server=True)
    with pytest.raises(TermctlError):
        open_cmd.check_size([over], TMUX, SOCK)


def test_size_check_judges_the_largest_invocation() -> None:
    small = tb.has_session("work")
    big = tb.new_window("work", spec("/" + "d" * 13000))
    with pytest.raises(TermctlError):
        open_cmd.check_size([small, big], TMUX, SOCK)


def test_window_spec_is_the_placeholder_until_the_pane_step_lands() -> None:
    # TODO(#15) changes this: the no-argv path gets the default shell and the -e variables.
    assert open_cmd.window_spec("n", "/w") == WindowSpec(
        "n", "/w", ("/usr/bin/env", "--", "sleep", "30")
    )


# --- step 5: server and session -----------------------------------------------------------------

SPEC = spec("/w", "KEY-12")


def key(invocation: tb.Invocation) -> tuple[str, ...]:
    return tuple(invocation.argv(TMUX, SOCK))


HAS = key(tb.has_session("work"))
START = key(tb.new_session("work", SPEC, start_server=True))
NEW_SESSION = key(tb.new_session("work", SPEC, start_server=False))
NEW_WINDOW = key(tb.new_window("work", SPEC))

NO_SERVER = completed(stderr=f"no server running on {SOCK}\n", returncode=1)
MISSING = completed(stderr="can't find session: work\n", returncode=1)
DUPLICATE = completed(stderr="duplicate session: work\n", returncode=1)
OK = completed()


class Script(FakeGateway):
    """A fake gateway whose tmux answers come from a per-argv queue, so a test can script the
    same invocation answering differently each time (a race)."""

    def __init__(
        self,
        script: Mapping[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.script = {k: list(v) for k, v in script.items()}

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


def no_socket(uid: int = UID) -> dict[str, object]:
    files = safe_default_server(uid)
    del files[SOCK]
    return {"files": files}


def test_existing_session_gets_a_new_window() -> None:
    gateway = Script({HAS: [OK], NEW_WINDOW: [completed("%5 @3\n")]})
    assert create(gateway) == Created("%5", "@3")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW]


def test_missing_session_is_created_without_the_server_prefix() -> None:
    # Spike F2: a running server's settings are never changed.
    gateway = Script({HAS: [MISSING], NEW_SESSION: [completed("%5 @3\n")]})
    assert create(gateway) == Created("%5", "@3")
    assert gateway.tmux_calls() == [HAS, NEW_SESSION]
    assert "exit-unattached" not in NEW_SESSION


def test_no_server_starts_one_with_the_prefix_and_confirms() -> None:
    gateway = Script({START: [completed("%0 @0\n")], HAS: [OK]}, **no_socket())
    assert create(gateway) == Created("%0", "@0")
    # No socket file: no probe before the start, has-session after it.
    assert gateway.tmux_calls() == [START, HAS]
    assert START[3:8] == ("set", "-s", "--", "exit-unattached", "off")


def test_a_stale_socket_file_is_no_server() -> None:
    gateway = Script({HAS: [NO_SERVER, OK], START: [completed("%0 @0\n")]})
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, START, HAS]


def test_a_started_server_that_is_not_there_is_server_unavailable() -> None:
    gateway = Script({START: [completed("%0 @0\n")], HAS: [NO_SERVER]}, **no_socket())
    assert "did not" in create_error(gateway).message


def test_a_started_server_without_the_session_is_server_unavailable() -> None:
    gateway = Script({START: [completed("%0 @0\n")], HAS: [MISSING]}, **no_socket())
    create_error(gateway)


def test_no_server_creates_the_default_directory_first() -> None:
    files = safe_default_server()
    del files[SOCK], files[f"/tmp/tmux-{UID}"]
    gateway = Script({START: [completed("%0 @0\n")], HAS: [OK]}, files=files)
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


def test_duplicate_session_race_falls_back_to_a_new_window() -> None:
    gateway = Script({HAS: [MISSING], NEW_SESSION: [DUPLICATE], NEW_WINDOW: [completed("%9 @9\n")]})
    assert create(gateway) == Created("%9", "@9")
    assert gateway.tmux_calls() == [HAS, NEW_SESSION, NEW_WINDOW]


def test_server_started_by_another_caller_falls_back_to_a_new_window() -> None:
    # The other caller's server has the session: the prefix ran there (a residual risk), and
    # termctl takes the new-window path without confirming a server it did not start.
    gateway = Script({START: [DUPLICATE], NEW_WINDOW: [completed("%9 @9\n")]}, **no_socket())
    assert create(gateway) == Created("%9", "@9")
    assert gateway.tmux_calls() == [START, NEW_WINDOW]


def test_vanished_session_is_retried_once_by_the_new_session_path() -> None:
    gateway = Script(
        {HAS: [OK, MISSING], NEW_WINDOW: [MISSING], NEW_SESSION: [completed("%2 @2\n")]}
    )
    assert create(gateway) == Created("%2", "@2")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, NEW_SESSION]


def test_vanished_session_and_server_restarts_the_server() -> None:
    gateway = Script(
        {HAS: [OK, NO_SERVER, OK], NEW_WINDOW: [MISSING], START: [completed("%0 @0\n")]}
    )
    assert create(gateway) == Created("%0", "@0")
    assert gateway.tmux_calls() == [HAS, NEW_WINDOW, HAS, START, HAS]


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


def test_server_vanishing_at_new_window_is_server_unavailable() -> None:
    gateway = Script({HAS: [OK], NEW_WINDOW: [NO_SERVER]})
    create_error(gateway)


def test_probe_failure_is_server_unavailable() -> None:
    gateway = Script(
        {HAS: [completed(stderr=f"error connecting to {SOCK} (Permission denied)\n", returncode=1)]}
    )
    create_error(gateway)
