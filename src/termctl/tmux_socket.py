"""Server selection, the canonical ``socket_path`` and the socket safety checks.

interface.md "Server selection" is normative; design.md D12 (always ``-S``) and D26 (directory
and ancestry checks before reaching a server) give the reasons. Every filesystem access goes
through the gateway.
"""

from __future__ import annotations

import errno
import posixpath
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from . import validate
from .errors import TermctlError, UsageError
from .gateway import FileInfo, Gateway

DEFAULT_NAME = "default"
DEFAULT_TMPDIR = "/tmp"

# Where the server came from, as it names the source in errors.
FLAG = "--socket"
ENV = "TERMCTL_SOCKET"
CONFIG = "config"
TMUX = "TMUX"
DEFAULT = "default"

_GROUP_OTHER_WRITE = 0o022
# tmux's own default directory rule, made stricter: no group write and no access by others.
_DEFAULT_DIR_FORBIDDEN = 0o027


@dataclass(frozen=True)
class Server:
    """The resolved tmux server."""

    socket_path: str
    """Canonical; passed to tmux as ``-S`` and reported in every output."""
    default_dir: str
    """``${TMUX_TMPDIR:-/tmp}/tmux-<uid>`` with its ancestors canonical (its last component is
    not resolved, so that a symlink there is caught by the checks)."""
    default_form: bool
    """Whether the socket's parent is the default directory (safety rule 1)."""
    source: str


def socket_args(server: Server) -> list[str]:
    """The tmux arguments selecting ``server``: always ``-S``, never ``-L``."""
    return ["-S", server.socket_path]


# --- resolution ---------------------------------------------------------------------------------


def resolve(
    flag: str | None,
    configured: str | None,
    environ: Mapping[str, str],
    gateway: Gateway,
) -> Server:
    """Resolve the server in the order ``--socket``, ``TERMCTL_SOCKET`` then config ``socket``,
    ``TMUX``, the default. Has no side effects: it reads the filesystem but creates nothing and
    runs no command.

    ``flag`` has passed ``validate.check_socket_flag`` and ``configured`` (the config ``socket``
    with ``TERMCTL_SOCKET`` already applied) the config grammar. Raises exit 2 errors:
    ``usage`` for the flag, ``config_invalid`` for config and the environment.
    """
    uid = gateway.real_uid()
    raw_default_dir = _raw_default_dir(environ, uid)
    default_dir = canonical_path(raw_default_dir, gateway)

    if flag is not None:
        source, value = FLAG, flag
    elif configured is not None:
        source, value = (ENV if ENV in environ else CONFIG), configured
    elif environ.get(TMUX):
        source, value = TMUX, _tmux_socket(environ[TMUX])
    else:
        source, value = DEFAULT, DEFAULT_NAME

    if validate.is_socket_path(value):
        given = value if value.startswith("/") else _join_cwd(value, gateway)
    else:
        given = f"{raw_default_dir}/{value}"
    socket_path = canonical_path(given, gateway)
    if problem := validate.socket_problem(socket_path, allow_relative=False):
        raise _invalid(source, f"socket path {validate.show(socket_path)}: {problem}", value)
    default_form = posixpath.dirname(socket_path) == default_dir or _lexical(
        posixpath.dirname(given)
    ) == _lexical(raw_default_dir)
    return Server(socket_path, default_dir, default_form, source)


def _raw_default_dir(environ: Mapping[str, str], uid: int) -> str:
    """``${TMUX_TMPDIR:-/tmp}/tmux-<uid>``. An empty ``TMUX_TMPDIR`` counts as unset, as in the
    shell form and in tmux. It must be absolute and give a valid socket directory."""
    tmpdir = environ.get("TMUX_TMPDIR") or DEFAULT_TMPDIR
    path = f"{tmpdir.rstrip('/')}/tmux-{uid}"
    problem = (
        "must be an absolute path"
        if not tmpdir.startswith("/")
        else validate.socket_problem(f"{path}/{DEFAULT_NAME}", allow_relative=False)
    )
    if problem:
        raise TermctlError(
            "config_invalid",
            f"invalid TMUX_TMPDIR {validate.show(tmpdir)}: {problem}",
            details={"variable": "TMUX_TMPDIR"},
        )
    return path


def _tmux_socket(value: str) -> str:
    """The socket path in ``TMUX``: its first comma-separated field, an absolute path."""
    path = value.split(",", 1)[0]
    problem = (
        "its first field must be an absolute socket path"
        if not path.startswith("/")
        else validate.socket_problem(path, allow_relative=False)
    )
    if problem:
        raise TermctlError(
            "config_invalid",
            f"invalid TMUX {validate.show(value)}: {problem}",
            details={"variable": TMUX},
        )
    return path


def _join_cwd(value: str, gateway: Gateway) -> str:
    try:
        base = gateway.current_dir()
    except OSError as exc:
        raise UsageError(f"cannot read the current directory: {exc.strerror or exc}") from None
    return posixpath.join(base, value)


def _invalid(source: str, message: str, value: str) -> TermctlError:
    """An invalid resolved socket path, an error of the source that named it."""
    if source == FLAG:
        return UsageError(f"invalid --socket {validate.show(value)}: {message}")
    if source == CONFIG:
        return TermctlError(
            "config_invalid", f"invalid config socket: {message}", details={"key": "socket"}
        )
    variable = {ENV: ENV, TMUX: TMUX, DEFAULT: "TMUX_TMPDIR"}[source]
    return TermctlError(
        "config_invalid", f"invalid {variable}: {message}", details={"variable": variable}
    )


def _parts(path: str) -> list[str]:
    """The components of ``path`` without empty and ``.`` ones, which never change what an
    absolute path names; ``..`` is kept."""
    return [part for part in path.split("/") if part not in ("", ".")]


def _lexical(path: str) -> str:
    return "/" + "/".join(_parts(path))


def canonical_path(path: str, gateway: Gateway) -> str:
    """The real path of the deepest existing ancestor directory of ``path``'s parent, joined
    with the rest of the parent's components and ``path``'s basename (never resolved).

    ``path`` is absolute. Each component is resolved as the kernel would: a symlink is
    followed, and ``..`` goes up from a real directory. The walk stops at the first component
    that is missing, cannot be read or resolved, or is not a directory; that component and the
    rest are kept as given (``..`` included), so the result names what the kernel would reach.
    The safety checks then fail on, or find missing, the first kept component.
    """
    *parents, base = _parts(path)
    current = "/"
    for index, part in enumerate(parents):
        if part == "..":
            current = posixpath.dirname(current)
            continue
        candidate = posixpath.join(current, part)
        try:
            info = gateway.lstat(candidate)
            if stat.S_ISLNK(info.mode):
                candidate = gateway.real_path(candidate)
                info = gateway.lstat(candidate)
        except OSError:
            return posixpath.join(current, *parents[index:], base)
        if not stat.S_ISDIR(info.mode):
            return posixpath.join(current, *parents[index:], base)
        current = candidate
    return posixpath.join(current, base)


# --- safety checks ------------------------------------------------------------------------------


def check(server: Server, gateway: Gateway) -> bool:
    """The socket safety checks, before termctl's first tmux operation.

    Returns True when the socket exists and passed, False for *no server* (the parent
    directory or the socket file is missing). Raises ``server_unavailable`` (exit 12) when a
    check fails. Every existing directory from ``/`` to the parent is checked first, so an
    unsafe ancestor fails even when the socket is missing.
    """
    uid = gateway.real_uid()
    parent = posixpath.dirname(server.socket_path)
    info = _check_ancestors(server, parent, uid, gateway)
    if info is None:
        return False
    if server.default_form:
        info = _lstat(server, server.default_dir, gateway)
        if info is None:
            return False
        _check_default_dir(server, server.default_dir, info, uid)
    else:
        _check_other_dir(server, parent, info, uid)

    info = _lstat(server, server.socket_path, gateway)
    if info is None:
        return False
    if not stat.S_ISSOCK(info.mode):
        raise _unsafe(server, server.socket_path, "it is not a socket")
    if info.uid != uid:
        raise _unsafe(server, server.socket_path, f"it is owned by uid {info.uid}, not {uid}")
    return True


def _check_ancestors(server: Server, parent: str, uid: int, gateway: Gateway) -> FileInfo | None:
    """Rule 4 for every existing directory from ``/`` down to ``parent``, top-down. Returns the
    parent's ``lstat``, or None at the first missing directory (nothing below it exists)."""
    current = "/"
    info = _lstat(server, current, gateway)
    for part in _parts(parent):
        if info is None:
            return None
        _check_ancestor(server, current, info, uid)
        if part == "..":
            raise _unsafe(server, current, "the socket path is not canonical")
        current = posixpath.join(current, part)
        info = _lstat(server, current, gateway)
    if info is not None:
        _check_ancestor(server, current, info, uid)
    return info


def _lstat(server: Server, path: str, gateway: Gateway) -> FileInfo | None:
    """``lstat`` of ``path``, None if it is missing."""
    try:
        return gateway.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = exc.strerror or errno.errorcode.get(exc.errno or 0, str(exc))
        raise _unsafe(server, path, f"cannot check it: {reason}") from None


def _require_directory(server: Server, path: str, info: FileInfo) -> None:
    if stat.S_ISLNK(info.mode):
        raise _unsafe(server, path, "it is a symlink")
    if not stat.S_ISDIR(info.mode):
        raise _unsafe(server, path, "it is not a directory")


def _check_ancestor(server: Server, path: str, info: FileInfo, uid: int) -> None:
    """Rule 4: owned by the uid or root; writable by group or others only when sticky."""
    _require_directory(server, path, info)
    if info.uid not in (uid, 0):
        raise _unsafe(server, path, f"it is owned by uid {info.uid}, not {uid} or root")
    if info.mode & _GROUP_OTHER_WRITE and not info.mode & stat.S_ISVTX:
        raise _unsafe(server, path, "it is writable by group or others and not sticky")


def _check_default_dir(server: Server, path: str, info: FileInfo, uid: int) -> None:
    """Rule 1: owned by the uid, not a symlink, ``mode & 0o027 == 0``."""
    _require_directory(server, path, info)
    if info.uid != uid:
        raise _unsafe(server, path, f"it is owned by uid {info.uid}, not {uid}")
    if info.mode & _DEFAULT_DIR_FORBIDDEN:
        raise _unsafe(
            server, path, f"its mode {stat.S_IMODE(info.mode):04o} allows group write or others"
        )


def _check_other_dir(server: Server, path: str, info: FileInfo, uid: int) -> None:
    """Rule 2: owned by the uid or root, not a symlink, no group or other write (a sticky bit
    is no exception)."""
    _require_directory(server, path, info)
    if info.uid not in (uid, 0):
        raise _unsafe(server, path, f"it is owned by uid {info.uid}, not {uid} or root")
    if info.mode & _GROUP_OTHER_WRITE:
        raise _unsafe(server, path, "it is writable by group or others")


def _unsafe(server: Server, path: str, problem: str) -> TermctlError:
    return TermctlError(
        "server_unavailable",
        f"unsafe tmux socket {validate.show(server.socket_path, 200)}: "
        f"{validate.show(path, 200)}: {problem}",
        details={"socket_path": server.socket_path, "path": path},
    )
