"""Server selection, the canonical socket_path and the socket safety checks
(interface.md "Server selection", design.md D12 and D26)."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from termctl import tmux_socket
from termctl.errors import TermctlError
from termctl.gateway import FileInfo, SystemGateway
from termctl.tmux_socket import Server

from .fakes import (
    UID,
    FakeGateway,
    directory,
    regular_file,
    safe_default_server,
    socket_file,
    symlink,
)

DEFAULT = f"/tmp/tmux-{UID}/default"


def resolve(
    flag: str | None = None,
    configured: str | None = None,
    environ: Mapping[str, str] | None = None,
    gateway: FakeGateway | None = None,
) -> Server:
    return tmux_socket.resolve(flag, configured, environ or {}, gateway or FakeGateway())


def resolve_error(
    flag: str | None = None,
    configured: str | None = None,
    environ: Mapping[str, str] | None = None,
    gateway: FakeGateway | None = None,
) -> TermctlError:
    with pytest.raises(TermctlError) as caught:
        resolve(flag, configured, environ, gateway)
    return caught.value


# --- server selection order ---------------------------------------------------------------------


def test_default_is_the_default_socket_in_the_default_directory() -> None:
    server = resolve()
    assert server.socket_path == DEFAULT
    assert server.source == "default"
    assert server.default_form


def test_tmux_names_the_server_by_its_first_field() -> None:
    server = resolve(environ={"TMUX": "/srv/tmux/s1,1234,0"})
    assert (server.socket_path, server.source) == ("/srv/tmux/s1", "TMUX")
    assert not server.default_form


def test_config_wins_over_tmux() -> None:
    server = resolve(configured="work", environ={"TMUX": "/srv/tmux/s1,1,0"})
    assert (server.socket_path, server.source) == (f"/tmp/tmux-{UID}/work", "config")


def test_termctl_socket_wins_over_tmux_and_is_reported_as_the_variable() -> None:
    # config.load has already put TERMCTL_SOCKET over the config file's `socket`.
    environ = {"TERMCTL_SOCKET": "/srv/env", "TMUX": "/srv/tmux/s1,1,0"}
    server = resolve(configured="/srv/env", environ=environ)
    assert (server.socket_path, server.source) == ("/srv/env", "TERMCTL_SOCKET")


def test_flag_wins_over_everything() -> None:
    environ = {"TERMCTL_SOCKET": "/srv/env", "TMUX": "/srv/tmux/s1,1,0"}
    server = resolve("/srv/flag", "/srv/env", environ)
    assert (server.socket_path, server.source) == ("/srv/flag", "--socket")


def test_flag_name_resolves_into_the_default_directory() -> None:
    server = resolve("work")
    assert server.socket_path == f"/tmp/tmux-{UID}/work"
    assert server.default_form


def test_name_uses_tmux_tmpdir_and_the_real_uid() -> None:
    gateway = FakeGateway(uid=4242, files=safe_default_server(4242, "/run/t"))
    server = resolve("work", environ={"TMUX_TMPDIR": "/run/t"}, gateway=gateway)
    assert server.socket_path == "/run/t/tmux-4242/work"
    assert server.default_dir == "/run/t/tmux-4242"
    assert server.default_form


def test_empty_tmux_tmpdir_means_tmp() -> None:
    assert resolve(environ={"TMUX_TMPDIR": ""}).socket_path == DEFAULT


@pytest.mark.parametrize("value", ["", ",1,0", ","])
def test_empty_tmux_or_one_starting_with_a_comma_is_not_inside_tmux(value: str) -> None:
    # As tmux itself treats it (tmux.c): no socket path in the first field means unset.
    server = resolve(environ={"TMUX": value})
    assert (server.socket_path, server.source) == (DEFAULT, "default")


def test_relative_flag_is_resolved_against_the_current_directory() -> None:
    gateway = FakeGateway(cwd="/home/u", files={"/": directory(), "/home": directory()})
    gateway.files["/home/u"] = directory(uid=UID)
    assert resolve("sockets/s", gateway=gateway).socket_path == "/home/u/sockets/s"


def test_relative_flag_without_a_current_directory_is_a_usage_error() -> None:
    err = resolve_error("rel/s", gateway=FakeGateway(cwd=None))
    assert err.code == "usage"


def test_socket_args_always_use_dash_s() -> None:
    server = resolve("work")
    assert tmux_socket.socket_args(server) == ["-S", f"/tmp/tmux-{UID}/work"]


def test_resolution_runs_nothing() -> None:
    gateway = FakeGateway()
    resolve("/srv/x", gateway=gateway)
    resolve(environ={"TMUX": "/a/b,1,0"}, gateway=gateway)
    assert gateway.calls == []


def test_tmux_is_not_read_when_a_socket_is_given() -> None:
    # An invalid TMUX does not matter when a higher-precedence source names the server.
    assert resolve("work", environ={"TMUX": "relative,1,0"}).source == "--socket"


# --- invalid environment values -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "relative/s,1,0",  # not absolute
        "name,1,0",  # not a path
        "/a/\x01b,1,0",  # control character
        "/a/\udcff,1,0",  # not valid Unicode
        "/a/b;,1,0",  # ends in ';'
        "/a/..,1,0",  # basename '..'
        "/a/,1,0",  # trailing '/'
        "/" + "a" * 100 + ",1,0",  # over 100 bytes as given
    ],
)
def test_invalid_tmux_is_config_invalid(value: str) -> None:
    err = resolve_error(environ={"TMUX": value})
    assert (err.code, err.exit_code) == ("config_invalid", 2)
    assert err.details == {"variable": "TMUX"}


INVALID_TMPDIRS = ["relative", "/a,b", "/a/\x07", "/a/\udc80", "/run/b:relative", "rel:/run/b"]


@pytest.mark.parametrize("value", INVALID_TMPDIRS)
@pytest.mark.parametrize("flag", [None, "work"])
def test_invalid_tmux_tmpdir_is_config_invalid_for_a_name_or_the_default(
    value: str, flag: str | None
) -> None:
    gateway = FakeGateway(files=safe_default_server(UID, "/run/b"))
    err = resolve_error(flag, environ={"TMUX_TMPDIR": value}, gateway=gateway)
    assert (err.code, err.exit_code) == ("config_invalid", 2)
    assert err.details == {"variable": "TMUX_TMPDIR"}


@pytest.mark.parametrize("value", [*INVALID_TMPDIRS, "/" + "x" * 95])
def test_invalid_tmux_tmpdir_does_not_matter_for_a_path(value: str) -> None:
    gateway = FakeGateway(files=other_path_files())
    environ = {"TMUX_TMPDIR": value}
    server = resolve("/srv/t/s", environ=environ, gateway=gateway)
    assert (server.socket_path, server.default_form) == ("/srv/t/s", False)
    assert check(server, gateway) is True
    environ["TMUX"] = "/srv/t/s,1,0"
    server = resolve(environ=environ, gateway=gateway)
    assert (server.socket_path, server.source, server.default_form) == ("/srv/t/s", "TMUX", False)


def test_tmux_tmpdir_length_is_checked_only_in_the_resolved_socket_path() -> None:
    tmpdir = "/" + "t" * 82
    gateway = FakeGateway(files=safe_default_server(UID, tmpdir))
    server = resolve("s", environ={"TMUX_TMPDIR": tmpdir}, gateway=gateway)
    assert server.socket_path == f"{tmpdir}/tmux-{UID}/s"
    assert len(server.socket_path.encode()) == 95
    assert server.default_form


# --- TMUX_TMPDIR as tmux reads it ---------------------------------------------------------------


def test_missing_tmux_tmpdir_falls_back_to_tmp() -> None:
    server = resolve(environ={"TMUX_TMPDIR": "/nonexistent"})
    assert (server.socket_path, server.default_dir) == (DEFAULT, f"/tmp/tmux-{UID}")


def test_tmux_tmpdir_that_is_not_a_directory_falls_back_to_tmp() -> None:
    gateway = FakeGateway()
    gateway.files["/f"] = regular_file()
    assert resolve(environ={"TMUX_TMPDIR": "/f"}, gateway=gateway).socket_path == DEFAULT


def test_tmux_tmpdir_list_uses_the_first_existing_directory() -> None:
    gateway = FakeGateway(files=safe_default_server(UID, "/run/b"))
    environ = {"TMUX_TMPDIR": "/run/a:/run/b"}
    assert resolve(environ=environ, gateway=gateway).socket_path == f"/run/b/tmux-{UID}/default"
    gateway.files["/run/a"] = directory()
    assert resolve(environ=environ, gateway=gateway).socket_path == f"/run/a/tmux-{UID}/default"


def test_tmux_tmpdir_empty_entries_are_skipped() -> None:
    gateway = FakeGateway(files=safe_default_server(UID, "/run/b"))
    server = resolve(environ={"TMUX_TMPDIR": "::/run/b:"}, gateway=gateway)
    assert server.socket_path == f"/run/b/tmux-{UID}/default"


def test_tmux_tmpdir_entry_through_a_symlink_is_followed() -> None:
    gateway = FakeGateway(files=safe_default_server(UID, "/run/b"), links={"/l": "/run/b"})
    server = resolve(environ={"TMUX_TMPDIR": "/l"}, gateway=gateway)
    assert server.socket_path == f"/run/b/tmux-{UID}/default"
    assert server.default_form


# --- the canonical socket_path ------------------------------------------------------------------


def test_symlinked_ancestors_are_resolved() -> None:
    gateway = FakeGateway(
        files={"/": directory(), "/real": directory(), "/real/dir": directory(uid=UID)},
        links={"/link": "/real"},
    )
    assert resolve("/link/dir/s", gateway=gateway).socket_path == "/real/dir/s"


def test_relative_symlink_target() -> None:
    gateway = FakeGateway(
        files={"/": directory(), "/a": directory(), "/a/real": directory()},
        links={"/a/link": "real"},
    )
    assert resolve("/a/link/s", gateway=gateway).socket_path == "/a/real/s"


def test_missing_components_are_kept_after_the_deepest_existing_ancestor() -> None:
    gateway = FakeGateway(files={"/": directory(), "/real": directory()}, links={"/link": "/real"})
    server = resolve("/link/missing/deeper/s", gateway=gateway)
    assert server.socket_path == "/real/missing/deeper/s"


def test_dotdot_after_a_missing_component_is_kept() -> None:
    # The kernel fails at the missing component, so it must not be cancelled out.
    gateway = FakeGateway(files={"/": directory(), "/a": directory()})
    assert resolve("/a/missing/../s", gateway=gateway).socket_path == "/a/missing/../s"


def test_dotdot_after_an_existing_directory_is_resolved() -> None:
    gateway = FakeGateway(
        files={"/": directory(), "/a": directory(), "/a/b": directory(), "/real": directory()},
        links={"/l": "/a/b"},
    )
    # /l/.. is the parent of the link's target, as the kernel resolves it.
    assert resolve("/l/../s", gateway=gateway).socket_path == "/a/s"


def test_dotdot_after_a_file_is_not_cancelled() -> None:
    gateway = FakeGateway(files={"/": directory(), "/a": directory(), "/a/f": regular_file()})
    assert resolve("/a/f/../s", gateway=gateway).socket_path == "/a/f/../s"


def test_dot_and_empty_components_are_dropped() -> None:
    gateway = FakeGateway(files={"/": directory(), "/a": directory()})
    assert resolve("//a/./b//s", gateway=gateway).socket_path == "/a/b/s"


def test_socket_basename_is_never_resolved() -> None:
    gateway = FakeGateway(
        files={"/": directory(), "/a": directory(), "/a/real": socket_file()},
        links={"/a/s": "real"},
    )
    assert resolve("/a/s", gateway=gateway).socket_path == "/a/s"


def test_socket_in_root() -> None:
    assert resolve("/s", gateway=FakeGateway(files={"/": directory()})).socket_path == "/s"


def test_dangling_symlink_is_kept_unresolved() -> None:
    gateway = FakeGateway(files={"/": directory()}, links={"/dangling": "/nowhere"})
    assert resolve("/dangling/s", gateway=gateway).socket_path == "/dangling/s"


def long_target_gateway() -> FakeGateway:
    target = "/" + "d" * 98
    return FakeGateway(files={"/": directory(), target: directory()}, links={"/l": target})


def test_canonical_path_over_100_bytes_from_the_flag_is_usage() -> None:
    err = resolve_error("/l/s", gateway=long_target_gateway())
    assert (err.code, err.exit_code) == ("usage", 2)
    assert "100 bytes" in err.message


def test_canonical_path_over_100_bytes_from_termctl_socket() -> None:
    err = resolve_error(None, "/l/s", {"TERMCTL_SOCKET": "/l/s"}, gateway=long_target_gateway())
    assert err.code == "config_invalid"
    assert err.details == {"variable": "TERMCTL_SOCKET"}


def test_canonical_path_over_100_bytes_from_config() -> None:
    err = resolve_error(None, "/l/s", gateway=long_target_gateway())
    assert err.code == "config_invalid"
    assert err.details == {"key": "socket"}


def test_canonical_path_over_100_bytes_from_tmux() -> None:
    err = resolve_error(environ={"TMUX": "/l/s,1,0"}, gateway=long_target_gateway())
    assert err.details == {"variable": "TMUX"}


def test_canonical_default_socket_over_100_bytes_names_tmux_tmpdir() -> None:
    tmpdir = "/" + "t" * 85
    gateway = FakeGateway(files={"/": directory()}, links={"/t": tmpdir})
    gateway.files[tmpdir] = directory(0o1777)
    err = resolve_error(environ={"TMUX_TMPDIR": "/t"}, gateway=gateway)
    assert err.code == "config_invalid"
    assert err.details == {"variable": "TMUX_TMPDIR"}


def test_exactly_100_bytes_is_accepted() -> None:
    path = "/" + "s" * 99
    assert len(path.encode()) == 100
    assert resolve(path, gateway=FakeGateway(files={"/": directory()})).socket_path == path


@pytest.mark.parametrize("target", ["/a,b", "/a\x1bb", "/a\u0085b"])
def test_canonical_path_must_follow_the_socket_rules(target: str) -> None:
    gateway = FakeGateway(files={"/": directory(), target: directory()}, links={"/l": target})
    err = resolve_error("/l/s", gateway=gateway)
    assert err.code == "usage"


# --- the default directory form -----------------------------------------------------------------


def test_explicit_path_in_the_default_directory_is_default_form() -> None:
    assert resolve(f"/tmp/tmux-{UID}/other").default_form


def test_tmux_path_in_the_default_directory_is_default_form() -> None:
    assert resolve(environ={"TMUX": f"/tmp/tmux-{UID}/default,1,0"}).default_form


def test_path_reaching_the_default_directory_through_a_symlink_is_default_form() -> None:
    gateway = FakeGateway(links={"/home": f"/tmp/tmux-{UID}"})
    server = resolve("/home/s", gateway=gateway)
    assert server.socket_path == f"/tmp/tmux-{UID}/s"
    assert server.default_form


def test_symlinked_tmpdir_is_compared_canonically() -> None:
    # As on macOS, where /tmp is a symlink to /private/tmp.
    files = safe_default_server(UID, "/private/tmp")
    gateway = FakeGateway(files=files, links={"/tmp": "/private/tmp"})
    server = resolve(environ={"TMUX": f"/private/tmp/tmux-{UID}/default,1,0"}, gateway=gateway)
    assert server.default_dir == f"/private/tmp/tmux-{UID}"
    assert server.default_form
    assert resolve(gateway=gateway).socket_path == f"/private/tmp/tmux-{UID}/default"


def test_other_uid_directory_is_not_default_form() -> None:
    assert not resolve("/tmp/tmux-0/default").default_form


def linked_default_dir_gateway() -> FakeGateway:
    """The default directory is a symlink to a safe-looking directory; ``/alias`` is ``/tmp``."""
    files = {"/": directory(), "/tmp": directory(0o1777), "/tmp/x": directory()}
    files["/elsewhere"] = directory(0o700, UID)
    files["/elsewhere/s"] = socket_file()
    links = {f"/tmp/tmux-{UID}": "/elsewhere", "/alias": "/tmp"}
    return FakeGateway(files=files, links=links)


@pytest.mark.parametrize("path", [f"/alias/tmux-{UID}/s", f"/tmp/x/../tmux-{UID}/s"])
def test_default_directory_reached_through_an_alias_or_dotdot_is_default_form(path: str) -> None:
    server = resolve(path, gateway=linked_default_dir_gateway())
    assert server.socket_path == "/elsewhere/s"
    assert server.default_form


# --- safety checks on a fake filesystem (owners the test user cannot create) --------------------


def check(server: Server, gateway: FakeGateway) -> bool:
    return tmux_socket.check(server, gateway)


def check_error(path: str, gateway: FakeGateway) -> TermctlError:
    server = resolve(path, gateway=gateway)
    with pytest.raises(TermctlError) as caught:
        check(server, gateway)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    assert caught.value.details is not None
    assert caught.value.details["socket_path"] == server.socket_path
    return caught.value


def test_safe_default_server_passes() -> None:
    gateway = FakeGateway()
    assert check(resolve(gateway=gateway), gateway) is True
    assert gateway.calls == []


def other_path_files(**parent: int) -> dict[str, FileInfo]:
    return {
        "/": directory(),
        "/srv": directory(),
        "/srv/t": directory(parent.get("mode", 0o755), parent.get("uid", UID)),
        "/srv/t/s": socket_file(),
    }


def test_other_path_owned_by_root_passes() -> None:
    gateway = FakeGateway(files=other_path_files(uid=0))
    assert check(resolve("/srv/t/s", gateway=gateway), gateway) is True


def test_other_path_owned_by_another_user_fails() -> None:
    err = check_error("/srv/t/s", FakeGateway(files=other_path_files(uid=UID + 1)))
    assert err.details is not None and err.details["path"] == "/srv/t"


@pytest.mark.parametrize("owner", [0, UID + 1])
def test_default_directory_owned_by_root_or_another_user_fails(owner: int) -> None:
    # Every ancestor passes, so this is rule 1's owner check.
    files = safe_default_server()
    files[f"/tmp/tmux-{UID}"] = directory(0o700, owner)
    err = check_error(DEFAULT, FakeGateway(files=files))
    assert err.details is not None and err.details["path"] == f"/tmp/tmux-{UID}"


def test_ancestor_owned_by_another_user_fails() -> None:
    files = other_path_files()
    files["/srv"] = directory(0o755, UID + 1)
    err = check_error("/srv/t/s", FakeGateway(files=files))
    assert err.details is not None and err.details["path"] == "/srv"


def test_root_owned_group_writable_ancestor_without_sticky_fails() -> None:
    files = other_path_files()
    files["/srv"] = directory(0o775, 0)
    check_error("/srv/t/s", FakeGateway(files=files))


def test_root_owned_sticky_world_writable_ancestor_passes() -> None:
    files = other_path_files()
    files["/srv"] = directory(0o1777, 0)
    gateway = FakeGateway(files=files)
    assert check(resolve("/srv/t/s", gateway=gateway), gateway) is True


def test_root_directory_is_checked_too() -> None:
    files = other_path_files()
    files["/"] = directory(0o777, 0)
    err = check_error("/srv/t/s", FakeGateway(files=files))
    assert err.details is not None and err.details["path"] == "/"


def test_socket_owned_by_another_user_fails() -> None:
    files = other_path_files()
    files["/srv/t/s"] = socket_file(uid=UID + 1)
    check_error("/srv/t/s", FakeGateway(files=files))


def test_socket_owned_by_root_fails() -> None:
    files = other_path_files()
    files["/srv/t/s"] = socket_file(uid=0)
    check_error("/srv/t/s", FakeGateway(files=files))


def test_parent_turned_symlink_after_resolution_fails() -> None:
    gateway = FakeGateway(files=other_path_files())
    server = resolve("/srv/t/s", gateway=gateway)
    del gateway.files["/srv/t"]
    gateway.links["/srv/t"] = "/elsewhere"
    with pytest.raises(TermctlError) as caught:
        check(server, gateway)
    assert caught.value.code == "server_unavailable"


def test_missing_socket_is_no_server() -> None:
    files = other_path_files()
    del files["/srv/t/s"]
    gateway = FakeGateway(files=files)
    assert check(resolve("/srv/t/s", gateway=gateway), gateway) is False


def test_missing_parent_is_no_server_and_stops_the_walk() -> None:
    gateway = FakeGateway(files={"/": directory(), "/srv": directory()})
    assert check(resolve("/srv/t/u/s", gateway=gateway), gateway) is False
    assert gateway.lstats[-1] == "/srv/t"


def test_missing_default_directory_is_no_server() -> None:
    gateway = FakeGateway(files={"/": directory(), "/tmp": directory(0o1777)})
    assert check(resolve(gateway=gateway), gateway) is False


def test_unsafe_ancestor_fails_before_a_missing_parent() -> None:
    files = {"/": directory(), "/srv": directory(0o777, 0)}
    err = check_error("/srv/missing/s", FakeGateway(files=files))
    assert err.details is not None and err.details["path"] == "/srv"


def test_dotdot_reached_in_the_walk_fails() -> None:
    gateway = FakeGateway(files={"/": directory(), "/a": directory()})
    server = resolve("/a/m/../s", gateway=gateway)
    gateway.files["/a/m"] = directory()  # created after resolution
    with pytest.raises(TermctlError) as caught:
        check(server, gateway)
    assert caught.value.code == "server_unavailable"


@pytest.mark.parametrize("path", [f"/alias/tmux-{UID}/s", f"/tmp/x/../tmux-{UID}/s"])
def test_symlinked_default_directory_through_an_alias_or_dotdot_fails(path: str) -> None:
    err = check_error(path, linked_default_dir_gateway())
    assert "symlink" in err.message
    assert err.details is not None and err.details["path"] == f"/tmp/tmux-{UID}"


@pytest.mark.parametrize("path", ["/home/l/s", f"/tmp/tmux-{UID}/sub/../s"])
def test_symlinked_default_directory_followed_as_a_link_fails(path: str) -> None:
    """The default directory followed as a link (here as the given parent's last component
    through ``/home/l``, or before a later ``..``) fails rule 1 though not in default form."""
    gateway = linked_default_dir_gateway()
    gateway.files["/home"] = directory(0o755, UID)
    gateway.files["/elsewhere/sub"] = directory(0o700, UID)
    gateway.links["/home/l"] = f"/tmp/tmux-{UID}"
    err = check_error(path, gateway)
    assert "symlink" in err.message
    assert err.details is not None and err.details["path"] == f"/tmp/tmux-{UID}"


def link_files(**extra: FileInfo) -> dict[str, FileInfo]:
    files = {"/": directory(), "/ok": directory(0o700, UID), "/ok/s": socket_file()}
    files.update({f"/{name}": info for name, info in extra.items()})
    return files


def test_link_owned_by_another_user_in_a_sticky_directory_fails() -> None:
    files = link_files(sticky=directory(0o1777))
    files["/sticky/l"] = symlink(uid=UID + 1)
    gateway = FakeGateway(files=files, links={"/sticky/l": "/ok"})
    err = check_error("/sticky/l/s", gateway)
    assert err.details is not None and err.details["path"] == "/sticky/l"


def test_link_in_a_group_writable_directory_fails() -> None:
    gateway = FakeGateway(files=link_files(shared=directory(0o775)), links={"/shared/l": "/ok"})
    err = check_error("/shared/l/s", gateway)
    assert err.details is not None and err.details["path"] == "/shared"


def test_link_in_an_unsafe_ancestor_of_its_directory_fails() -> None:
    files = link_files(shared=directory(0o777))
    files["/shared/d"] = directory(0o755, UID)
    gateway = FakeGateway(files=files, links={"/shared/d/l": "/ok"})
    err = check_error("/shared/d/l/s", gateway)
    assert err.details is not None and err.details["path"] == "/shared"


def test_every_link_of_a_chain_is_checked() -> None:
    # /home/u/l is safe, but leads through /shared/l2, in a directory others can write.
    files = link_files(home=directory(), shared=directory(0o777))
    files["/home/u"] = directory(0o755, UID)
    links = {"/home/u/l": "/shared/l2", "/shared/l2": "/ok"}
    err = check_error("/home/u/l/s", FakeGateway(files=files, links=links))
    assert err.details is not None and err.details["path"] == "/shared"


def test_link_owned_by_the_user_in_their_own_directory_passes() -> None:
    files = link_files(home=directory())
    files["/home/u"] = directory(0o755, UID)
    gateway = FakeGateway(files=files, links={"/home/u/l": "/ok"})
    server = resolve("/home/u/l/s", gateway=gateway)
    assert server.socket_path == "/ok/s"
    assert check(server, gateway) is True


def test_root_owned_link_passes() -> None:
    # As on macOS: /tmp is a root-owned symlink to private/tmp.
    files = safe_default_server(UID, "/private/tmp")
    files["/tmp"] = symlink(uid=0)
    gateway = FakeGateway(files=files, links={"/tmp": "private/tmp"})
    server = resolve(f"/tmp/tmux-{UID}/default", gateway=gateway)
    assert server.socket_path == f"/private/tmp/tmux-{UID}/default"
    assert check(server, gateway) is True


def test_link_replaced_after_resolution_fails() -> None:
    files = link_files(home=directory())
    files["/home/u"] = directory(0o755, UID)
    gateway = FakeGateway(files=files, links={"/home/u/l": "/ok"})
    server = resolve("/home/u/l/s", gateway=gateway)
    del gateway.links["/home/u/l"]
    gateway.files["/home/u/l"] = directory(0o700, UID)
    with pytest.raises(TermctlError) as caught:
        check(server, gateway)
    assert caught.value.details is not None
    assert caught.value.details["path"] == "/home/u/l"


# --- safety checks on the real filesystem -------------------------------------------------------


class RealFs(SystemGateway):
    """The real filesystem, with a real uid the test can change to play another user."""

    def __init__(self, uid: int | None = None) -> None:
        self.uid = os.getuid() if uid is None else uid

    def real_uid(self) -> int:
        return self.uid


@pytest.fixture
def base() -> Iterator[Path]:
    """A short directory under /tmp (a sticky, root-owned ancestor), so socket paths fit in
    100 bytes; pytest's tmp_path can be longer."""
    path = Path(tempfile.mkdtemp(prefix="tc-", dir="/tmp"))
    try:
        yield path
    finally:
        for root, dirs, _ in os.walk(path):
            for name in dirs:
                if not os.path.islink(os.path.join(root, name)):
                    os.chmod(os.path.join(root, name), 0o700)
        shutil.rmtree(path)


def make_socket(path: Path) -> Path:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    return path


def real_check(path: str | None, environ: Mapping[str, str], gw: RealFs | None = None) -> bool:
    gateway = gw or RealFs()
    server = tmux_socket.resolve(path, None, environ, gateway)
    return tmux_socket.check(server, gateway)


def real_failure(
    path: str | None, environ: Mapping[str, str], gw: RealFs | None = None
) -> TermctlError:
    with pytest.raises(TermctlError) as caught:
        real_check(path, environ, gw)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    assert caught.value.details is not None
    return caught.value


def real_error(path: str | None, environ: Mapping[str, str], gw: RealFs | None = None) -> str:
    return real_failure(path, environ, gw).message


def failed_path(err: TermctlError) -> str:
    assert err.details is not None
    return err.details["path"]


def default_dir(base: Path, mode: int = 0o700) -> Path:
    path = base / f"tmux-{os.getuid()}"
    path.mkdir()
    path.chmod(mode)
    return path


def test_real_default_server_passes(base: Path) -> None:
    make_socket(default_dir(base) / "default")
    assert real_check(None, {"TMUX_TMPDIR": str(base)}) is True


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o740, 0o710])
def test_real_default_directory_modes_accepted(base: Path, mode: int) -> None:
    make_socket(default_dir(base, mode) / "default")
    assert real_check("default", {"TMUX_TMPDIR": str(base)}) is True


@pytest.mark.parametrize("mode", [0o720, 0o770, 0o701, 0o704, 0o702, 0o1770, 0o777])
def test_real_default_directory_modes_rejected(base: Path, mode: int) -> None:
    make_socket(default_dir(base, mode) / "default")
    real_error("default", {"TMUX_TMPDIR": str(base)})


def test_real_default_directory_symlink_fails(base: Path) -> None:
    target = base / "target"
    target.mkdir(mode=0o700)
    make_socket(target / "default")
    (base / f"tmux-{os.getuid()}").symlink_to(target)
    message = real_error(None, {"TMUX_TMPDIR": str(base)})
    assert "symlink" in message


def test_real_missing_default_directory_is_no_server_and_not_created(base: Path) -> None:
    assert real_check(None, {"TMUX_TMPDIR": str(base)}) is False
    assert list(base.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o700, 0o555])
def test_real_other_path_modes_accepted(base: Path, mode: int) -> None:
    parent = base / "p"
    parent.mkdir()
    make_socket(parent / "s")
    parent.chmod(mode)
    assert real_check(str(parent / "s"), {}) is True


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o1777, 0o1775, 0o3777])
def test_real_other_path_writable_parent_rejected_even_when_sticky(base: Path, mode: int) -> None:
    parent = base / "p"
    parent.mkdir()
    make_socket(parent / "s")
    parent.chmod(mode)
    real_error(str(parent / "s"), {})


def test_real_sticky_writable_ancestor_is_accepted(base: Path) -> None:
    (base / "shared").mkdir()
    (base / "shared").chmod(0o1777)
    parent = base / "shared" / "p"
    parent.mkdir(mode=0o700)
    make_socket(parent / "s")
    assert real_check(str(parent / "s"), {}) is True


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757])
def test_real_writable_ancestor_without_sticky_fails(base: Path, mode: int) -> None:
    (base / "shared").mkdir()
    (base / "shared").chmod(mode)
    parent = base / "shared" / "p"
    parent.mkdir(mode=0o700)
    make_socket(parent / "s")
    err = real_failure(str(parent / "s"), {})
    assert failed_path(err) == str(base / "shared")


def test_real_ancestor_owned_by_another_user_fails(base: Path) -> None:
    parent = base / "p"
    parent.mkdir(mode=0o700)
    make_socket(parent / "s")
    # Played as another user, `base` (ours) is an ancestor owned by someone else.
    err = real_failure(str(parent / "s"), {}, RealFs(os.getuid() + 1))
    assert failed_path(err) == str(base)


def test_real_unsafe_ancestor_fails_even_without_a_socket(base: Path) -> None:
    (base / "shared").mkdir()
    (base / "shared").chmod(0o777)
    real_error(str(base / "shared" / "missing" / "s"), {})


def test_real_symlinked_ancestor_is_checked_at_its_target(base: Path) -> None:
    (base / "shared").mkdir()
    (base / "shared").chmod(0o777)
    parent = base / "shared" / "p"
    parent.mkdir(mode=0o700)
    make_socket(parent / "s")
    (base / "safe-looking").symlink_to(base / "shared")
    real_error(str(base / "safe-looking" / "p" / "s"), {})


def linked_default_dir(base: Path) -> Path:
    """A TMUX_TMPDIR whose tmux-<uid> is a symlink to a safe-looking directory with a socket."""
    tmpdir = base / "t"
    tmpdir.mkdir()
    tmpdir.chmod(0o755)
    (tmpdir / "x").mkdir()
    target = base / "target"
    target.mkdir(mode=0o700)
    make_socket(target / "s")
    (tmpdir / f"tmux-{os.getuid()}").symlink_to(target)
    (base / "alias").symlink_to(tmpdir)
    return tmpdir


@pytest.mark.parametrize("via", ["alias/{d}/s", "t/x/../{d}/s"])
def test_real_symlinked_default_directory_reached_indirectly_fails(base: Path, via: str) -> None:
    tmpdir = linked_default_dir(base)
    path = f"{base}/{via.format(d=f'tmux-{os.getuid()}')}"
    err = real_failure(path, {"TMUX_TMPDIR": str(tmpdir)})
    assert "symlink" in err.message
    assert failed_path(err) == str(tmpdir / f"tmux-{os.getuid()}")


@pytest.mark.parametrize("via", ["home/l/s", "t/{d}/sub/../s"])
def test_real_symlinked_default_directory_followed_as_a_link_fails(base: Path, via: str) -> None:
    tmpdir = linked_default_dir(base)
    name = f"tmux-{os.getuid()}"
    (base / "target" / "sub").mkdir(mode=0o700)
    (base / "home").mkdir(mode=0o755)
    (base / "home" / "l").symlink_to(tmpdir / name)
    err = real_failure(f"{base}/{via.format(d=name)}", {"TMUX_TMPDIR": str(tmpdir)})
    assert "symlink" in err.message
    assert failed_path(err) == str(tmpdir / name)


def test_real_link_in_a_group_writable_directory_fails(base: Path) -> None:
    ok = base / "ok"
    ok.mkdir(mode=0o700)
    make_socket(ok / "s")
    shared = base / "shared"
    shared.mkdir()
    (shared / "l").symlink_to(ok)
    shared.chmod(0o775)
    err = real_failure(str(shared / "l" / "s"), {})
    assert failed_path(err) == str(shared)


def test_real_socket_symlink_fails(base: Path) -> None:
    parent = base / "p"
    parent.mkdir(mode=0o700)
    make_socket(parent / "real")
    (parent / "s").symlink_to(parent / "real")
    real_error(str(parent / "s"), {})


def test_real_regular_file_socket_fails(base: Path) -> None:
    parent = base / "p"
    parent.mkdir(mode=0o700)
    (parent / "s").write_text("")
    message = real_error(str(parent / "s"), {})
    assert "not a socket" in message


def test_real_parent_is_a_file_fails(base: Path) -> None:
    (base / "f").write_text("")
    real_error(str(base / "f" / "s"), {})


def test_real_dangling_symlink_in_the_path_fails(base: Path) -> None:
    (base / "dangling").symlink_to(base / "nowhere")
    real_error(str(base / "dangling" / "s"), {})


def test_real_symlink_loop_in_the_path_fails(base: Path) -> None:
    (base / "loop").symlink_to(base / "loop")
    real_error(str(base / "loop" / "s"), {})


def test_real_missing_socket_and_parent_are_no_server(base: Path) -> None:
    (base / "p").mkdir(mode=0o700)
    assert real_check(str(base / "p" / "s"), {}) is False
    assert real_check(str(base / "missing" / "s"), {}) is False
    assert not (base / "missing").exists()


def test_real_unsearchable_directory_fails(base: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root bypasses directory permissions")
    parent = base / "p" / "q"
    parent.mkdir(parents=True, mode=0o700)
    (base / "p").chmod(0o000)
    message = real_error(str(parent / "s"), {})
    assert "cannot" in message


def test_real_tmp_is_an_accepted_sticky_ancestor() -> None:
    info = os.lstat("/tmp")
    if info.st_uid != 0 or not info.st_mode & 0o1000:
        pytest.skip("/tmp is not a root-owned sticky directory here")
    assert real_check("/tmp/tc-no-such-dir-for-termctl-tests/s", {}) is False


# --- before open starts a server ----------------------------------------------------------------


def no_default_dir(uid: int = UID) -> dict[str, FileInfo]:
    files = safe_default_server(uid)
    del files[f"/tmp/tmux-{uid}/default"], files[f"/tmp/tmux-{uid}"]
    return files


def prepare(gateway: FakeGateway, flag: str | None = None) -> bool:
    return tmux_socket.prepare_start(resolve(flag, gateway=gateway), gateway)


def prepare_error(gateway: FakeGateway, flag: str | None = None) -> TermctlError:
    with pytest.raises(TermctlError) as caught:
        prepare(gateway, flag)
    assert (caught.value.code, caught.value.exit_code) == ("server_unavailable", 12)
    return caught.value


def test_prepare_creates_a_missing_default_directory_with_mode_0700() -> None:
    gateway = FakeGateway(files=no_default_dir())
    assert prepare(gateway) is False
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]


def test_prepare_creates_the_default_directory_for_a_socket_name() -> None:
    gateway = FakeGateway(files=no_default_dir())
    assert prepare(gateway, "work") is False
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]


def test_prepare_rechecks_an_existing_directory_after_eexist() -> None:
    files = safe_default_server()
    del files[DEFAULT]
    gateway = FakeGateway(files=files)
    assert prepare(gateway) is False
    # mkdir is always attempted; EEXIST leaves the directory as it is, checked again.
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]
    assert gateway.files[f"/tmp/tmux-{UID}"] == directory(0o700, UID)


def test_prepare_reports_an_existing_socket() -> None:
    gateway = FakeGateway()
    assert prepare(gateway) is True
    assert gateway.mkdirs == []


def test_prepare_checks_a_directory_that_appeared_meanwhile() -> None:
    # EEXIST: someone else created it between the lstat and the mkdir; it is checked again.
    gateway = FakeGateway(files=no_default_dir())
    gateway.appears[f"/tmp/tmux-{UID}"] = directory(0o705, UID)
    message = prepare_error(gateway).message
    assert "allows group write or others" in message
    assert len(gateway.mkdirs) == 1


def test_prepare_accepts_a_safe_directory_that_appeared_meanwhile() -> None:
    gateway = FakeGateway(files=no_default_dir())
    gateway.appears[f"/tmp/tmux-{UID}"] = directory(0o700, UID)
    assert prepare(gateway) is False
    assert len(gateway.mkdirs) == 1


def test_prepare_refuses_a_default_directory_that_appeared_as_a_symlink() -> None:
    gateway = FakeGateway(files=no_default_dir())
    gateway.appears[f"/tmp/tmux-{UID}"] = symlink(UID)
    assert "symlink" in prepare_error(gateway).message


def test_prepare_refuses_a_default_directory_that_appeared_owned_by_another_user() -> None:
    gateway = FakeGateway(files=no_default_dir())
    gateway.appears[f"/tmp/tmux-{UID}"] = directory(0o700, UID + 1)
    assert "owned by uid" in prepare_error(gateway).message


class LateLstat(FakeGateway):
    """A fake gateway where ``path`` appears (with ``info``) just before its ``nth`` lstat:
    another process creating it between two of termctl's looks. Resolution looks once and
    the first check once, so ``nth=3`` is right after the first check."""

    def __init__(self, path: str, nth: int, info: FileInfo, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.late = (path, nth, info)

    def lstat(self, path: str) -> FileInfo:
        late_path, nth, info = self.late
        if path == late_path and self.lstats.count(path) + 1 == nth:
            self.files[path] = info
        return super().lstat(path)


@pytest.mark.parametrize(
    ("info", "problem"),
    [(symlink(UID), "symlink"), (directory(0o777, UID), "writable by group or others")],
    ids=["symlink", "mode-0777"],
)
def test_prepare_rechecks_a_default_directory_that_appeared_after_the_first_check(
    info: FileInfo, problem: str
) -> None:
    # The directory is missing at the first check and appears right after it.
    gateway = LateLstat(f"/tmp/tmux-{UID}", 3, info, files=no_default_dir())
    assert problem in prepare_error(gateway).message


def test_prepare_accepts_a_safe_default_directory_that_appeared_after_the_first_check() -> None:
    gateway = LateLstat(f"/tmp/tmux-{UID}", 3, directory(0o700, UID), files=no_default_dir())
    assert prepare(gateway) is False


def test_prepare_rechecks_after_creating_the_directory() -> None:
    # A socket that appears with the directory is reported: the checks run after the mkdir.
    gateway = FakeGateway(files=no_default_dir())
    gateway.appears[DEFAULT] = socket_file()
    assert prepare(gateway) is True
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]


def test_prepare_explicit_path_rechecks_a_parent_that_appeared_after_the_first_check() -> None:
    files = safe_default_server()
    gateway = LateLstat("/srv", 3, directory(0o777, UID), files=files)
    assert "writable by group or others" in prepare_error(gateway, "/srv/sock").message


def test_prepare_mkdir_failure_is_server_unavailable() -> None:
    gateway = FakeGateway(files=no_default_dir())
    gateway.mkdir_error = PermissionError(13, "Permission denied")
    err = prepare_error(gateway)
    assert "Permission denied" in err.message
    assert err.details == {"socket_path": DEFAULT}


def test_prepare_explicit_path_with_a_missing_parent_is_server_unavailable() -> None:
    gateway = FakeGateway()
    err = prepare_error(gateway, "/srv/missing/sock")
    assert "/srv/missing" in err.message
    assert err.details == {"socket_path": "/srv/missing/sock"}
    assert gateway.mkdirs == []


def test_prepare_explicit_path_in_the_default_directory_creates_it() -> None:
    # A path given explicitly but in the default directory form is treated as that form.
    gateway = FakeGateway(files=no_default_dir())
    assert prepare(gateway, f"/tmp/tmux-{UID}/mine") is False
    assert gateway.mkdirs == [(f"/tmp/tmux-{UID}", 0o700)]


def test_prepare_never_creates_a_directory_under_an_unsafe_ancestor() -> None:
    files = no_default_dir()
    files["/tmp"] = directory(0o777)  # writable by others, not sticky
    gateway = FakeGateway(files=files)
    prepare_error(gateway)
    assert gateway.mkdirs == []


def test_real_prepare_creates_the_default_directory(base: Path) -> None:
    gateway = RealFs()
    server = tmux_socket.resolve(None, None, {"TMUX_TMPDIR": str(base)}, gateway)
    old = os.umask(0o022)
    try:
        assert tmux_socket.prepare_start(server, gateway) is False
    finally:
        os.umask(old)
    created = base / f"tmux-{os.getuid()}"
    assert stat.S_IMODE(created.lstat().st_mode) == 0o700
    # A second call finds it and creates nothing.
    assert tmux_socket.prepare_start(server, gateway) is False


def test_real_prepare_explicit_missing_parent_creates_nothing(base: Path) -> None:
    gateway = RealFs()
    server = tmux_socket.resolve(str(base / "missing" / "s"), None, {}, gateway)
    with pytest.raises(TermctlError) as caught:
        tmux_socket.prepare_start(server, gateway)
    assert caught.value.exit_code == 12
    assert list(base.iterdir()) == []
