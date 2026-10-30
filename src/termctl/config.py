"""Config: the config file, ``config.d/`` drop-ins and ``TERMCTL_*`` scalars.

interface.md "Config", "Environment", "Placement" and "Hooks" are normative; the models match
``schemas/config.json``. Reading the config files directly (not through the gateway) follows
design.md "Internal architecture": the gateway owns every filesystem access outside config and
logs.
"""

from __future__ import annotations

import copy
import os
import pwd
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import TermctlError

# Every key with its built-in default; equal to config.example.yaml (tested).
DEFAULTS: dict[str, Any] = {
    "backend": "tmux",
    "tmux_bin": "tmux",
    "socket": None,
    "server_env_allow": [
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_*",
        "TERM",
        "COLORTERM",
        "TZ",
        "XDG_*",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    ],
    "keep_on_exit": "never",
    "close_grace": "10s",
    "marker_display": "name",
    "markers": {"attention": {"text": "🔔 ", "bell": False}},
    "placements": {"default": {"session": "termctl", "target": "window"}},
    "rules": [],
    "hooks": {},
}

# The top-level scalars that TERMCTL_<KEY> may set (interface.md "Environment").
ENV_KEYS = ("socket", "tmux_bin", "backend", "keep_on_exit", "close_grace", "marker_display")

PRE_HOOKS = ("pre-open", "pre-close")


class ConfigError(TermctlError):
    """Invalid config (exit 2, ``config_invalid``); ``details`` names the key, variable or file."""

    def __init__(self, message: str, **details: str) -> None:
        super().__init__("config_invalid", message, details=details)


# --- grammars (schemas/config.json) -------------------------------------------------------------
# TODO: share these with validate.py once issue #4 lands its grammars; kept here so config
# loading does not depend on that issue. Keep both in step with schemas/config.json.

_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")
_SOCKET_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DURATION = re.compile(
    r"(0|[1-9][0-9]{0,4}|[1-5][0-9]{5}|600000)ms|(0|[1-9][0-9]?|[1-5][0-9]{2}|600)s?|([0-9]|10)m"
)
_MARKER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_PLACEMENT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SESSION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_LABEL_VALUE = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}")
_ENV_ALLOW = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\*?")


def _grammar(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"not a valid {what}: {value!r}")
        return value

    return AfterValidator(check)


def check_socket(value: str) -> str:
    """A socket name (not ``.`` or ``..``) or an absolute socket path (interface.md
    "Server selection"). Relative paths are allowed only for ``--socket``."""
    if "/" not in value:
        if not _SOCKET_NAME.fullmatch(value) or value in (".", ".."):
            raise ValueError(
                f"not a socket name ([A-Za-z0-9._-], 1 to 64, not . or ..) "
                f"or an absolute path: {value!r}"
            )
    elif not value.startswith("/"):
        raise ValueError(f"a socket path must be absolute here: {value!r}")
    elif _CONTROL.search(value):
        raise ValueError("a socket path must contain no control characters")
    elif value.endswith(";"):
        raise ValueError(f"a socket path must not end in ';': {value!r}")
    elif value.endswith("/"):
        raise ValueError(f"a socket path must have a basename: {value!r}")
    if len(value) > 100:
        raise ValueError("a socket path must be at most 100 characters")
    return value


def _check_marker_text(value: str) -> str:
    if not 1 <= len(value) <= 16:
        raise ValueError("a marker text must be 1 to 16 characters")
    if _CONTROL.search(value) or "#[" in value or value.endswith("#"):
        raise ValueError(
            f"a marker text must contain no control characters or '#[' "
            f"and must not end in '#': {value!r}"
        )
    return value


NonEmpty = Annotated[str, Field(min_length=1)]
Socket = Annotated[str, AfterValidator(check_socket)]
Duration = Annotated[str, _grammar(_DURATION, "duration (e.g. 10s, 500ms, 2m; at most 10m)")]
MarkerName = Annotated[str, _grammar(_MARKER_NAME, "marker name")]
MarkerText = Annotated[str, AfterValidator(_check_marker_text)]
PlacementName = Annotated[str, _grammar(_PLACEMENT_NAME, "placement name")]
SessionName = Annotated[str, _grammar(_SESSION_NAME, "session name")]
LabelKey = Annotated[str, _grammar(_LABEL_KEY, "label key")]
LabelValue = Annotated[str, _grammar(_LABEL_VALUE, "label value")]
EnvAllow = Annotated[str, _grammar(_ENV_ALLOW, "variable name or PREFIX* glob")]


# --- models -------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Marker(_Model):
    text: MarkerText
    bell: bool = False


class Placement(_Model):
    """A configured placement. Fields left out are ``None`` (and absent from
    ``model_fields_set``): they inherit from ``default`` at resolution time.

    ``target: pane``, ``max_panes``, ``layout`` and ``group_by`` load without error;
    ``open`` and ``placements`` reject them when they resolve such a placement.
    """

    session: SessionName | None = None
    target: Literal["window", "pane"] | None = None
    max_panes: Annotated[int, Field(ge=1)] | None = None
    layout: str | None = None
    group_by: str | None = None

    @field_validator("session", "target", "max_panes", "layout", mode="before")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("must not be null")
        return value


class Rule(_Model):
    match: dict[LabelKey, LabelValue]
    placement: PlacementName


class PreHook(_Model):
    cmd: NonEmpty
    timeout: Annotated[int, Field(ge=1)] = 30


class PostHook(PreHook):
    async_: bool = Field(default=False, alias="async")


class Hooks(_Model):
    pre_open: list[PreHook] = Field(default_factory=list[PreHook], alias="pre-open")
    pre_close: list[PreHook] = Field(default_factory=list[PreHook], alias="pre-close")
    post_open: list[PostHook] = Field(default_factory=list[PostHook], alias="post-open")
    post_close: list[PostHook] = Field(default_factory=list[PostHook], alias="post-close")


class Config(_Model):
    """The effective config. Every key is required here because loading always merges the
    files over ``DEFAULTS``; use ``defaults()`` for the built-in config."""

    backend: Literal["tmux"]
    tmux_bin: NonEmpty
    socket: Socket | None
    server_env_allow: list[EnvAllow]
    keep_on_exit: Literal["never", "failed", "always"]
    close_grace: Duration
    marker_display: Literal["name", "option"]
    markers: dict[MarkerName, Marker]
    placements: dict[PlacementName, Placement]
    rules: list[Rule]
    hooks: Hooks


def defaults() -> Config:
    """The built-in config."""
    return Config.model_validate(copy.deepcopy(DEFAULTS))


# --- loading ------------------------------------------------------------------------------------


def config_path(environ: Mapping[str, str], explicit: str | None) -> Path:
    """``--config PATH``, else ``$XDG_CONFIG_HOME/termctl/config.yaml`` (when set and
    absolute), else ``~/.config/termctl/config.yaml``."""
    if explicit is not None:
        return Path(explicit)
    xdg = environ.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        home = environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
        base = Path(home) / ".config"
    return base / "termctl" / "config.yaml"


def merge(base: Mapping[Any, Any], over: Mapping[Any, Any], path: tuple[Any, ...] = ()) -> Any:
    """Merge ``over`` into a copy of ``base``: maps key by key at every depth, ``hooks`` lists
    appended, every other value (other lists included) replaced."""
    result = dict(base)
    for key, value in over.items():
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = merge(old, value, (*path, key))  # pyright: ignore[reportUnknownArgumentType]
        elif path == ("hooks",) and isinstance(old, list) and isinstance(value, list):
            result[key] = old + value
        else:
            result[key] = value
    return result


def _read(path: Path) -> dict[Any, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "not UTF-8"
        raise ConfigError(f"cannot read config {path}: {reason}", file=str(path)) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid config {path}: {exc}", file=str(path)) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"invalid config {path}: the top level must be a mapping", file=str(path))
    return data  # pyright: ignore[reportUnknownVariableType]


def _dropins(main: Path) -> list[Path]:
    """``config.d/*.yaml`` next to the main file, in lexical order; hidden files are skipped,
    as a shell glob would."""
    directory = main.parent / "config.d"
    if not directory.is_dir():
        return []
    paths = [p for p in directory.glob("*.yaml") if not p.name.startswith(".")]
    return sorted(paths, key=lambda p: p.name)


def _key(loc: tuple[int | str, ...]) -> str:
    """Render a pydantic error location as ``hooks.pre-open[0].cmd``."""
    key = ""
    for part in loc:
        if part == "[key]":
            continue
        key += f"[{part}]" if isinstance(part, int) else (f".{part}" if key else str(part))
    return key


def _first_error(exc: ValidationError) -> tuple[str, str]:
    error = exc.errors()[0]
    message = error["msg"].removeprefix("Value error, ")
    return _key(error["loc"]), message


def _validate(data: Mapping[Any, Any], on_error: Callable[[str, str], ConfigError]) -> Config:
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise on_error(*_first_error(exc)) from None


def _key_error(key: str, message: str) -> ConfigError:
    return ConfigError(f"invalid config: {key}: {message}", key=key)


def _check_pre_hooks(data: Mapping[Any, Any]) -> None:
    """``async`` is allowed only on post-hooks."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for name in PRE_HOOKS:
        entries = hooks.get(name)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if isinstance(entry, dict) and "async" in entry:
                raise _key_error(
                    f"hooks.{name}[{index}].async", "async is allowed only on post-hooks"
                )


def _check_rules(cfg: Config) -> None:
    """Every rule names a defined placement."""
    for index, rule in enumerate(cfg.rules):
        if rule.placement not in cfg.placements:
            raise _key_error(f"rules[{index}].placement", f"undefined placement {rule.placement!r}")


def load(environ: Mapping[str, str], explicit: str | None = None) -> Config:
    """Load the effective config: built-in defaults, the main file, the drop-ins in lexical
    order, then ``TERMCTL_<KEY>`` scalars. Raises ``ConfigError`` naming the key, variable or
    file. A missing main file is an error only when given with ``--config``."""
    main = config_path(environ, explicit)
    data: dict[Any, Any] = copy.deepcopy(DEFAULTS)
    if explicit is not None or main.exists():
        data = merge(data, _read(main))
    for path in _dropins(main):
        data = merge(data, _read(path))

    _check_pre_hooks(data)
    _validate(data, _key_error)

    for key in ENV_KEYS:
        variable = f"TERMCTL_{key.upper()}"
        if variable in environ:
            data[key] = environ[variable]
            _validate(
                data,
                lambda _key, message, var=variable: ConfigError(
                    f"invalid {var}: {message}", variable=var
                ),
            )

    cfg = _validate(data, _key_error)
    _check_rules(cfg)
    return cfg
