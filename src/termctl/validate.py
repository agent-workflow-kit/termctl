"""Input grammars (interface.md "Names", "Labels", "Markers", "Placement", "Server selection",
"Hooks", "Audit and log", `open`, `focus` and `close`).

Grammars that config loading (``config_invalid``) or the pane reader also need have a predicate
(``is_*``, or ``*_problem`` returning a message); command-line input goes through a
``check_*``/``parse_*`` function that raises ``UsageError`` (exit 2). termctl rejects invalid
input and never cleans it.
"""

from __future__ import annotations

import os
import re

from .errors import UsageError
from .gateway import Gateway
from .output import escape

# C0 (tab and newline included), DEL and C1.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Lone surrogates: bytes that were not valid UTF-8 in argv (Python's surrogateescape).
_SURROGATE = re.compile(r"[\ud800-\udfff]")

_MARKER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_LABEL_VALUE = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}")
_SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PLACEMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DURATION = re.compile(r"(0|[1-9][0-9]*)(ms|s|m)?")
_ACTOR = re.compile(r"(human|auto):[A-Za-z0-9._@-]{1,64}")
_ACTOR_USER = re.compile(r"[A-Za-z0-9._@-]{1,64}")
_DECISION_ID = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
_CLIENT = re.compile(r"/?[A-Za-z0-9_./-]{1,100}")
_SOCKET_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")

NAME_MAX = 64
MARKER_TEXT_MAX = 16
SOCKET_PATH_MAX_BYTES = 100
MAX_LABELS = 32
MAX_ENV = 32
MAX_DECISION_IDS = 32
DURATION_MAX_MS = 600_000
# The most characters of a value ``show`` prints before cutting it.
SHOW_LIMIT = 64
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, None: 1000}


def show(value: str, limit: int = SHOW_LIMIT) -> str:
    """``value`` quoted for a message: control characters escaped, overlong input cut."""
    if len(value) > limit:
        return f"'{escape(value[:limit])}'... ({len(value)} characters)"
    return f"'{escape(value)}'"


def has_control(value: str) -> bool:
    """Whether ``value`` has a C0 (tab and newline included), DEL or C1 control character."""
    return _CONTROL.search(value) is not None


def has_surrogate(value: str) -> bool:
    """Whether ``value`` has a lone surrogate, so is not valid Unicode text."""
    return _SURROGATE.search(value) is not None


def _fullmatch(pattern: re.Pattern[str], value: str) -> bool:
    return pattern.fullmatch(value) is not None


def _text_problem(value: str, max_len: int) -> str | None:
    """The name rules: 1 to ``max_len`` code points, no control characters, no ``#[``."""
    if not 1 <= len(value) <= max_len:
        return f"must be 1 to {max_len} characters"
    if _CONTROL.search(value):
        return "must not contain control characters"
    if _SURROGATE.search(value):
        return "must be valid Unicode text"
    if "#[" in value:
        return "must not contain '#['"
    return None


# --- names and markers --------------------------------------------------------------------------


def name_problem(value: str) -> str | None:
    return _text_problem(value, NAME_MAX)


def check_name(value: str) -> str:
    problem = name_problem(value)
    if problem:
        raise UsageError(f"invalid name {show(value)}: {problem}")
    return value


def is_marker_name(value: str) -> bool:
    return _fullmatch(_MARKER_NAME, value)


def check_marker_name(value: str) -> str:
    if not is_marker_name(value):
        raise UsageError(
            f"invalid marker name {show(value)}: must match [a-z0-9][a-z0-9_-]{{0,31}}"
        )
    return value


def marker_text_problem(value: str) -> str | None:
    problem = _text_problem(value, MARKER_TEXT_MAX)
    if problem is None and value.endswith("#"):
        problem = "must not end in '#'"
    return problem


# --- labels -------------------------------------------------------------------------------------


def is_label_key(value: str) -> bool:
    return _fullmatch(_LABEL_KEY, value)


def is_label_value(value: str) -> bool:
    return _fullmatch(_LABEL_VALUE, value)


def parse_label(arg: str) -> tuple[str, str]:
    """Split ``key=value`` at the first ``=`` and check both parts."""
    key, sep, value = arg.partition("=")
    if not sep:
        raise UsageError(f"invalid label {show(arg)}: expected key=value")
    if not is_label_key(key):
        raise UsageError(f"invalid label key {show(key)}: must match [a-z0-9][a-z0-9_.-]{{0,63}}")
    if not is_label_value(value):
        raise UsageError(
            f"invalid label value {show(value)} for {key!r}: "
            "must be 0 to 256 characters from [A-Za-z0-9._:/#@+=,-]"
        )
    return key, value


def parse_labels(args: list[str]) -> list[str]:
    """Labels as a set of ``key=value`` pairs, sorted by code point."""
    return sorted({"=".join(parse_label(arg)) for arg in args})


def check_label_count(pairs: list[str]) -> None:
    if len(pairs) > MAX_LABELS:
        raise UsageError(f"too many labels: {len(pairs)} (at most {MAX_LABELS})")


# --- sessions, placements, clients --------------------------------------------------------------


def is_session_name(value: str) -> bool:
    return _fullmatch(_SESSION_NAME, value)


def check_session_name(value: str) -> str:
    if not is_session_name(value):
        raise UsageError(f"invalid session name {show(value)}: must match [A-Za-z0-9_-]{{1,64}}")
    return value


def check_session_filter(value: str) -> str:
    """``list --session`` matches sessions as tmux reports them: any non-empty string."""
    if not value:
        raise UsageError("--session must not be empty")
    return value


def is_placement_name(value: str) -> bool:
    return _fullmatch(_PLACEMENT_NAME, value)


def check_placement_name(value: str) -> str:
    if not is_placement_name(value):
        raise UsageError(
            f"invalid placement name {show(value)}: must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    return value


def is_client(value: str) -> bool:
    return _fullmatch(_CLIENT, value)


def check_client(value: str) -> str:
    if not is_client(value):
        raise UsageError(f"invalid client {show(value)}: must match /?[A-Za-z0-9_./-]{{1,100}}")
    return value


# --- env ----------------------------------------------------------------------------------------


def is_env_name(value: str) -> bool:
    return _fullmatch(_ENV_NAME, value)


def parse_env(args: list[str]) -> dict[str, str]:
    """``--env VAR=value`` arguments, split at the first ``=``. Values are never echoed."""
    if len(args) > MAX_ENV:
        raise UsageError(f"too many --env: {len(args)} (at most {MAX_ENV})")
    env: dict[str, str] = {}
    for arg in args:
        name, sep, value = arg.partition("=")
        if not sep or not is_env_name(name):
            raise UsageError(
                f"invalid --env {show(name)}: expected VAR=value with VAR matching "
                "[A-Za-z_][A-Za-z0-9_]*"
            )
        if name in env:
            raise UsageError(f"--env {name} given twice")
        env[name] = value
    return env


# --- durations ----------------------------------------------------------------------------------


def duration_ms(value: str) -> int | None:
    """A DURATION in milliseconds, or None if it breaks the grammar or exceeds ``10m``."""
    match = _DURATION.fullmatch(value)
    if match is None or len(match.group(1)) > 7:
        return None
    ms = int(match.group(1)) * _UNIT_MS[match.group(2)]
    return ms if ms <= DURATION_MAX_MS else None


def parse_duration(value: str) -> int:
    ms = duration_ms(value)
    if ms is None:
        raise UsageError(
            f"invalid duration {show(value)}: expected an integer without leading zeros "
            "followed by ms, s or m (bare: seconds), at most 10m"
        )
    return ms


# --- actor and decision ids ---------------------------------------------------------------------


def check_actor(value: str) -> str:
    if not _fullmatch(_ACTOR, value):
        raise UsageError(
            f"invalid actor {show(value)}: must match (human|auto):[A-Za-z0-9._@-]{{1,64}}"
        )
    return value


def default_actor(*, stdin_is_tty: bool, user: str | None) -> tuple[str, str | None]:
    """The actor when ``--actor`` is not given, and a warning to print, if any."""
    if not stdin_is_tty:
        return "auto:unknown", None
    if user is None:
        return "human:unknown", "the OS user has no name; using actor human:unknown"
    if _fullmatch(_ACTOR_USER, user):
        return f"human:{user}", None
    return "human:unknown", (
        "the OS user name does not fit the actor grammar; using actor human:unknown"
    )


def resolve_actor(given: str | None, gateway: Gateway) -> tuple[str, str | None]:
    """``--actor`` if given, else the default; returns the actor and a warning, if any.

    No caller yet: the ``--override-escalation`` gate and audit records of ``open`` (#17) and
    the ``close --force`` gate (#23) will call it.
    """
    if given is not None:
        return check_actor(given), None
    return default_actor(stdin_is_tty=gateway.stdin_isatty(), user=gateway.user_name())


def is_decision_id(value: str) -> bool:
    return _fullmatch(_DECISION_ID, value)


def check_decision_ids(values: list[str]) -> list[str]:
    if len(values) > MAX_DECISION_IDS:
        raise UsageError(f"too many --decision-id: {len(values)} (at most {MAX_DECISION_IDS})")
    for value in values:
        if not is_decision_id(value):
            raise UsageError(
                f"invalid decision id {show(value)}: must match [A-Za-z0-9._:@/-]{{1,128}}"
            )
    return values


# --- sockets ------------------------------------------------------------------------------------


def is_socket_path(value: str) -> bool:
    """A socket value containing ``/`` is a path; any other value is a socket name."""
    return "/" in value


def socket_problem(value: str, *, allow_relative: bool) -> str | None:
    """Why ``value`` is not a valid ``--socket`` (``allow_relative``) or config/env socket.

    Only the grammar: resolution and canonicalization come later, and the canonical path is
    checked again then.
    """
    if not is_socket_path(value):
        if value in (".", "..") or not _fullmatch(_SOCKET_NAME, value):
            return "a socket name must match [A-Za-z0-9._-]{1,64} and not be '.' or '..'"
        return None
    if not allow_relative and not value.startswith("/"):
        return "must be a socket name or an absolute path"
    if _CONTROL.search(value):
        return "a socket path must not contain control characters"
    if _SURROGATE.search(value):
        return "a socket path must be valid Unicode text"
    if "," in value:
        return "a socket path must not contain ','"
    if value.endswith(";"):
        return "a socket path must not end in ';'"
    if value.rpartition("/")[2] in ("", ".", ".."):
        return "a socket path must have a basename other than '.' and '..'"
    if len(value.encode()) > SOCKET_PATH_MAX_BYTES:
        return f"a socket path must be at most {SOCKET_PATH_MAX_BYTES} bytes"
    return None


def check_socket_flag(value: str) -> str:
    problem = socket_problem(value, allow_relative=True)
    if problem:
        raise UsageError(f"invalid --socket {show(value)}: {problem}")
    return value


# --- cwd ----------------------------------------------------------------------------------------


def cwd_problem(path: str) -> str | None:
    if _CONTROL.search(path):
        return "must not contain control characters"
    if _SURROGATE.search(path):
        return "must be valid Unicode text"
    if "#[" in path:
        return "must not contain '#['"
    return None


def check_cwd_flag(value: str) -> str:
    """The text of ``--cwd`` as given; ``resolve_cwd`` checks the resolved path."""
    if value == "":
        raise UsageError("--cwd must not be empty")
    if problem := cwd_problem(value):
        raise UsageError(f"invalid --cwd {show(value)}: {problem}")
    return value


def resolve_cwd(value: str | None, gateway: Gateway) -> str:
    """``open``'s working directory: ``--cwd`` against the caller's directory, else that one.

    ``value`` has passed ``check_cwd_flag``. The caller's directory is read only when ``value``
    is absent or relative. The result is joined, not normalized, so tmux gets the path the
    caller gave.
    """
    if value is not None and os.path.isabs(value):
        path = value
    else:
        try:
            base = gateway.current_dir()
        except OSError as exc:
            raise UsageError(f"cannot read the current directory: {exc.strerror or exc}") from None
        path = base if value is None else os.path.join(base, value)
    if problem := cwd_problem(path):
        raise UsageError(f"invalid working directory {show(path)}: {problem}")
    if not gateway.is_dir(path):
        raise UsageError(f"working directory {show(path)} is not an existing directory")
    return path
