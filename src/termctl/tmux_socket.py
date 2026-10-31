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
# The most symlinks followed in one path, as the kernel's limit (Linux MAXSYMLINKS).
_MAX_LINKS = 40


@dataclass(frozen=True)
class Server:
    """The resolved tmux server."""

    socket_path: str
    """Canonical; passed to tmux as ``-S`` and reported in every output."""
    default_dir: str | None
    """``<tmpdir>/tmux-<uid>`` (``_tmpdir``) with its ancestors canonical (its last component
    is not resolved, so that a symlink there is caught by the checks). None when the socket
    was given as a path and ``TMUX_TMPDIR`` is invalid: then nothing is in default form."""
    default_form: bool
    """Whether the socket's parent is the default directory (safety rule 1)."""
    source: str
    links: tuple[str, ...] = ()
    """The symlinks followed while canonicalizing the socket path, in order; each is in a real
    directory. The checks require them and their directories to be safe."""


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
    if flag is not None:
        source, value = FLAG, flag
    elif configured is not None:
        source, value = (ENV if ENV in environ else CONFIG), configured
    elif _inside_tmux(environ):
        source, value = TMUX, _tmux_socket(environ[TMUX])
    else:
        source, value = DEFAULT, DEFAULT_NAME
    is_path = validate.is_socket_path(value)

    # An invalid TMUX_TMPDIR matters only to a name (or the default): a path just has no
    # default form.
    try:
        raw_default_dir: str | None = f"{_tmpdir(environ, gateway).rstrip('/')}/tmux-{uid}"
    except TermctlError:
        if not is_path:
            raise
        raw_default_dir = None
    default_dir = None if raw_default_dir is None else canonical_path(raw_default_dir, gateway)

    if is_path:
        given = value if value.startswith("/") else _join_cwd(value, gateway)
    else:
        given = f"{raw_default_dir}/{value}"
    links: list[str] = []
    socket_path = canonical_path(given, gateway, links)
    if problem := validate.socket_problem(socket_path, allow_relative=False):
        raise _invalid(source, f"socket path {validate.show(socket_path)}: {problem}", value)
    default_form = _in_default_dir(socket_path, given, default_dir, gateway)
    return Server(socket_path, default_dir, default_form, source, tuple(links))


def _inside_tmux(environ: Mapping[str, str]) -> bool:
    """Whether ``TMUX`` names a server. Empty, or starting with ``,`` (no socket path), it
    counts as unset, as in tmux."""
    value = environ.get(TMUX, "")
    return value != "" and not value.startswith(",")


def _tmpdir(environ: Mapping[str, str], gateway: Gateway) -> str:
    """The directory of the default sockets, as tmux picks it: the first entry of the
    ``:``-separated ``TMUX_TMPDIR`` that is an existing directory (symlinks followed), else
    ``/tmp``. Empty entries are skipped. Raises ``config_invalid`` if any entry is relative or
    has a control character, a ``,`` or invalid Unicode (termctl does not resolve a relative
    entry, which tmux would)."""
    value = environ.get("TMUX_TMPDIR", "")
    entries = [entry for entry in value.split(":") if entry]
    for entry in entries:
        if problem := _tmpdir_problem(entry):
            raise TermctlError(
                "config_invalid",
                f"invalid TMUX_TMPDIR {validate.show(value)}: {validate.show(entry)} {problem}",
                details={"variable": "TMUX_TMPDIR"},
            )
    for entry in entries:
        try:
            if stat.S_ISDIR(gateway.lstat(gateway.real_path(entry)).mode):
                return entry
        except OSError:
            continue
    return DEFAULT_TMPDIR


def _tmpdir_problem(entry: str) -> str | None:
    if not entry.startswith("/"):
        return "must be an absolute path"
    if validate.has_control(entry):
        return "must not contain control characters"
    if validate.has_surrogate(entry):
        return "must be valid Unicode text"
    if "," in entry:
        return "must not contain ','"
    return None


def _in_default_dir(socket_path: str, given: str, default_dir: str | None, gw: Gateway) -> bool:
    """Whether the socket's parent is the default directory: its canonical parent is, or the
    given parent canonicalized as the default directory is (last component unresolved), so a
    symlinked default directory reached through an alias or ``..`` is still default form."""
    if default_dir is None:
        return False
    if posixpath.dirname(socket_path) == default_dir:
        return True
    parent = posixpath.dirname(given)
    return bool(_parts(parent)) and canonical_path(parent, gw) == default_dir


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


def canonical_path(path: str, gateway: Gateway, links: list[str] | None = None) -> str:
    """The real path of the deepest existing ancestor directory of ``path``'s parent, joined
    with the rest of the parent's components and ``path``'s basename (never resolved).

    ``path`` is absolute. Each component is resolved as the kernel would: a symlink is
    followed, and ``..`` goes up from a real directory. The walk stops at the first component
    that is missing, cannot be read or resolved, or is not a directory; that component and the
    rest are kept as given (``..`` included), so the result names what the kernel would reach.
    The safety checks then fail on, or find missing, the first kept component. Every symlink
    followed for the components that were resolved is appended to ``links``.
    """
    followed: list[str] = [] if links is None else links
    *parents, base = _parts(path)
    current = "/"
    for index, part in enumerate(parents):
        if part == "..":
            current = posixpath.dirname(current)
            continue
        mark = len(followed)
        try:
            current = _enter(current, part, gateway, followed)
        except OSError:
            del followed[mark:]
            return posixpath.join(current, *parents[index:], base)
    return posixpath.join(current, base)


def _enter(current: str, part: str, gateway: Gateway, followed: list[str]) -> str:
    """The real directory that ``part`` of the real directory ``current`` leads to, following
    symlinks one at a time and appending each to ``followed``. Raises ``OSError`` when a
    component is missing, cannot be read, is not a directory, or there are too many links."""
    candidate = posixpath.join(current, part)
    info = gateway.lstat(candidate)
    if stat.S_ISLNK(info.mode):
        if len(followed) >= _MAX_LINKS:
            raise OSError(errno.ELOOP, "too many levels of symbolic links", candidate)
        followed.append(candidate)
        target = gateway.read_link(candidate)
        reached = "/" if target.startswith("/") else current
        for step in _parts(target):
            if step == "..":
                reached = posixpath.dirname(reached)
            else:
                reached = _enter(reached, step, gateway, followed)
        return reached
    if not stat.S_ISDIR(info.mode):
        raise NotADirectoryError(errno.ENOTDIR, "not a directory", candidate)
    return candidate


# --- safety checks ------------------------------------------------------------------------------


def check(server: Server, gateway: Gateway) -> bool:
    """The socket safety checks, before termctl's first tmux operation.

    Returns True when the socket exists and passed, False for *no server* (the parent
    directory or the socket file is missing). Raises ``server_unavailable`` (exit 12) when a
    check fails. Every existing directory from ``/`` to the parent is checked first, so an
    unsafe ancestor fails even when the socket is missing.
    """
    uid = gateway.real_uid()
    for link in dict.fromkeys(server.links):
        _check_link(server, link, uid, gateway)
    if server.default_dir is not None and server.default_dir in server.links:
        # The walk followed the default directory as a link: rule 1 fails however the path
        # was given (as a later link's target, or before a ``..``).
        raise _unsafe(server, server.default_dir, "it is a symlink")
    parent = posixpath.dirname(server.socket_path)
    info = _check_ancestors(server, parent, uid, gateway)
    if info is None:
        return False
    if server.default_form:
        assert server.default_dir is not None
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


def _check_link(server: Server, link: str, uid: int, gateway: Gateway) -> None:
    """A symlink followed in resolution: still a symlink, owned by the uid or root, and its
    directory and that directory's ancestors pass rule 4."""
    info = _lstat(server, link, gateway)
    if info is None or not stat.S_ISLNK(info.mode):
        raise _unsafe(server, link, "it is no longer the symlink the socket path was resolved by")
    if info.uid not in (uid, 0):
        raise _unsafe(server, link, f"it is a symlink owned by uid {info.uid}, not {uid} or root")
    if _check_ancestors(server, posixpath.dirname(link), uid, gateway) is None:
        raise _unsafe(server, link, "its directory is missing")


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


# --- before open starts a server ----------------------------------------------------------------

DEFAULT_DIR_MODE = 0o700


def prepare_start(server: Server, gateway: Gateway) -> bool:
    """Make the socket's directory ready for ``open`` to start a server (interface.md "Open
    order" step 5), with the safety checks run before and after.

    A missing parent directory is created only in the default directory form: ``mkdir`` with
    mode 0700, never its parents. If it appeared meanwhile (``EEXIST``), the checks run on it
    as they find it. For any other path a missing parent exits 12 (``server_unavailable``),
    since tmux would exit 0 there without a server (spike F6). Returns what ``check`` returns
    after: True when a socket file is now there (a stale one, or a server started meanwhile).
    """
    if check(server, gateway):
        return True
    parent = posixpath.dirname(server.socket_path)
    if _lstat(server, parent, gateway) is not None:
        return False
    if not server.default_form:
        raise _cannot_start(server, f"its directory {validate.show(parent, 200)} does not exist")
    assert server.default_dir is not None
    try:
        gateway.make_dir(server.default_dir, DEFAULT_DIR_MODE)
    except FileExistsError:
        pass
    except OSError as exc:
        reason = exc.strerror or errno.errorcode.get(exc.errno or 0, str(exc))
        raise _cannot_start(
            server, f"cannot create {validate.show(server.default_dir, 200)}: {reason}"
        ) from None
    return check(server, gateway)


def _cannot_start(server: Server, problem: str) -> TermctlError:
    return TermctlError(
        "server_unavailable",
        f"cannot start a tmux server on {validate.show(server.socket_path, 200)}: {problem}",
        details={"socket_path": server.socket_path},
    )
