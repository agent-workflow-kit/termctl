"""Config: the config file, ``config.d/`` drop-ins and ``TERMCTL_*`` scalars.

interface.md "Config", "Environment", "Placement" and "Hooks" are normative; the models match
``schemas/config.json``. Reading the config files directly (not through the gateway) follows
design.md "Internal architecture": the gateway owns every filesystem access outside config and
logs.
"""

from __future__ import annotations

import copy
import errno
import os
import pwd
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from . import validate
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

DEFAULT_HOOK_TIMEOUT_S = 30
MAX_HOOK_TIMEOUT_S = 600


class ConfigError(TermctlError):
    """Invalid config (exit 2, ``config_invalid``); ``details`` names the key, variable or file."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("config_invalid", message, details=details)


# --- grammars (schemas/defs.json, through schemas/config.json) ----------------------------------
# The predicates are validate.py's, so config and the command line share one grammar each.


def _grammar(is_valid: Callable[[str], bool], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not is_valid(value):
            raise ValueError(f"not a valid {what}: {validate.show(value)}")
        return value

    return AfterValidator(check)


def _problem(problem: Callable[[str], str | None], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if message := problem(value):
            raise ValueError(f"not a valid {what} {validate.show(value)}: {message}")
        return value

    return AfterValidator(check)


def _config_socket_problem(value: str) -> str | None:
    """Config and ``TERMCTL_SOCKET`` take a socket name or an absolute path; relative paths
    are allowed only for ``--socket`` (interface.md "Server selection")."""
    return validate.socket_problem(value, allow_relative=False)


def _is_duration(value: str) -> bool:
    return validate.duration_ms(value) is not None


def _is_env_allow(value: str) -> bool:
    """A variable name, or a ``PREFIX*`` glob whose prefix is one."""
    return validate.is_env_name(value.removesuffix("*"))


def _integral(value: Any) -> Any:
    """Accept an integral float (``1.0``) as an integer, as JSON Schema does."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


NonEmpty = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, BeforeValidator(_integral), Field(ge=1)]
# A hook timeout in whole seconds (interface.md "Hooks"): at most 10 minutes, like every other
# duration termctl accepts, which leaves room for a gate with agent checks (design guide
# section 8 gives such a gate 600 s).
HookTimeout = Annotated[int, BeforeValidator(_integral), Field(ge=1, le=MAX_HOOK_TIMEOUT_S)]
Socket = Annotated[str, _problem(_config_socket_problem, "socket")]
Duration = Annotated[str, _grammar(_is_duration, "duration (e.g. 10s, 500ms, 2m; at most 10m)")]
MarkerName = Annotated[str, _grammar(validate.is_marker_name, "marker name")]
MarkerText = Annotated[str, _problem(validate.marker_text_problem, "marker text")]
PlacementName = Annotated[str, _grammar(validate.is_placement_name, "placement name")]
SessionName = Annotated[str, _grammar(validate.is_session_name, "session name")]
LabelKey = Annotated[str, _grammar(validate.is_label_key, "label key")]
LabelValue = Annotated[str, _grammar(validate.is_label_value, "label value")]
EnvAllow = Annotated[str, _grammar(_is_env_allow, "variable name or PREFIX* glob")]


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
    max_panes: PositiveInt | None = None
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
    timeout: HookTimeout = DEFAULT_HOOK_TIMEOUT_S


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

_HOME_UNKNOWN = "cannot determine the home directory; set HOME or XDG_CONFIG_HOME, or use --config"


def config_path(environ: Mapping[str, str], explicit: str | None) -> Path:
    """``--config PATH``, else ``$XDG_CONFIG_HOME/termctl/config.yaml`` (when set and
    absolute), else ``~/.config/termctl/config.yaml`` (an absolute ``HOME``, else the passwd
    entry's home, which must be absolute)."""
    if explicit is not None:
        return Path(explicit)
    base = xdg_base(environ, "XDG_CONFIG_HOME", ".config")
    if base is None:
        raise ConfigError(_HOME_UNKNOWN)
    return base / "termctl" / "config.yaml"


def home_dir(environ: Mapping[str, str]) -> Path | None:
    """An absolute ``HOME``, else the passwd entry's home if it is absolute, else None."""
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            return None
        if not os.path.isabs(home):
            return None
    return Path(home)


def xdg_base(environ: Mapping[str, str], variable: str, fallback: str) -> Path | None:
    """``$variable`` when it is set and absolute (XDG base directory spec), else
    ``<home>/<fallback>``; None when the home directory cannot be determined."""
    value = environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    home = home_dir(environ)
    return None if home is None else home / fallback


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


class _Rejected(yaml.MarkedYAMLError):
    """A YAML construct config files do not allow; ``key`` is the dotted path, if any."""

    def __init__(self, message: str, mark: yaml.Mark | None, key: str | None = None) -> None:
        super().__init__(problem=message, problem_mark=mark)
        self.key = key


_TAG_PREFIX = "tag:yaml.org,2002:"
_STR_TAG = f"{_TAG_PREFIX}str"

# The YAML 1.2 core schema's scalar forms, by tag: the implicit resolvers and the explicit-tag
# constructors both match these (in full), so ``!!int`` accepts exactly what ``17`` resolves as.
_CORE_FORMS = {
    "bool": re.compile(r"true|True|TRUE|false|False|FALSE"),
    "int": re.compile(r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+"),
    "float": re.compile(
        r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    ),
    "null": re.compile(r"~|null|Null|NULL|"),
}
_FIRST_CHARACTERS = {
    "bool": "tTfF",
    "int": "-+0123456789",
    "float": "-+.0123456789",
    "null": ["~", "n", "N", ""],
}


def _short_tag(tag: str) -> str:
    return f"!!{tag.removeprefix(_TAG_PREFIX)}" if tag.startswith(_TAG_PREFIX) else tag


def _is_merge_key(node: yaml.Node) -> bool:
    """A plain ``<<`` key: a merge key in YAML 1.1, rejected rather than read as a string."""
    return isinstance(node, yaml.ScalarNode) and node.value == "<<" and node.style is None


def _check_keys(root: yaml.Node, tagged: set[int]) -> None:
    """Every mapping key is a string, appears once, and is not a merge key (``<<``); the first
    violation in document order is reported. ``tagged`` holds the ids of explicitly tagged
    nodes: a tagged scalar key's hint is to remove the tag rather than to quote. A sequence or
    mapping key names the mapping it is in (it has no text of its own)."""
    stack: list[tuple[yaml.Node, str]] = [(root, "")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, yaml.SequenceNode):
            items: list[yaml.Node] = node.value
            children = [(item, f"{path}[{index}]") for index, item in enumerate(items)]
            stack.extend(reversed(children))
        elif isinstance(node, yaml.MappingNode):
            pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
            seen: set[str] = set()
            values: list[tuple[yaml.Node, str]] = []
            for key_node, value_node in pairs:
                if _is_merge_key(key_node):
                    raise _Rejected("merge keys (<<) are not allowed", key_node.start_mark)
                if not isinstance(key_node, yaml.ScalarNode):
                    kind = "sequence" if isinstance(key_node, yaml.SequenceNode) else "mapping"
                    problem = f"a {kind} key is not allowed; keys must be strings"
                    if path:
                        raise _Rejected(
                            f"{validate.show(path)}: {problem}", key_node.start_mark, path
                        )
                    raise _Rejected(problem, key_node.start_mark)
                text: str = key_node.value
                key = f"{path}.{text}" if path else text
                if key_node.tag != _STR_TAG:
                    if id(key_node) in tagged:
                        hint = f"the key is tagged {_short_tag(key_node.tag)}, not a string;"
                        hint += " remove the tag"
                    else:
                        kind = key_node.tag.rsplit(":", 1)[-1]
                        article = "an" if kind[:1] in ("a", "e", "i", "o", "u") else "a"
                        hint = f"the key is read as {article} {kind}, not a string;"
                        hint += f" quote it ({validate.show(text)})"
                    raise _Rejected(f"{validate.show(key)}: {hint}", key_node.start_mark, key)
                if text in seen:
                    raise _Rejected(
                        f"{validate.show(key)}: duplicate key", key_node.start_mark, key
                    )
                seen.add(text)
                values.append((value_node, key))
            stack.extend(reversed(values))


def _core_scalar(loader: yaml.SafeLoader, node: yaml.ScalarNode, kind: str, what: str) -> str:
    """The text of ``node``, which must be the core schema's form of ``kind``."""
    value = loader.construct_scalar(node)
    if not _CORE_FORMS[kind].fullmatch(value):
        raise _Rejected(f"not {what}: {validate.show(value)}", node.start_mark)
    return value


def _construct_bool(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> bool:
    return _core_scalar(loader, node, "bool", "a boolean (use true or false)").lower() == "true"


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    value = _core_scalar(loader, node, "int", "an integer")
    try:
        return int(value, 0) if value[:2] in ("0o", "0x") else int(value, 10)
    except ValueError:  # more digits than Python converts
        raise _Rejected(f"integer too large: {validate.show(value)}", node.start_mark) from None


def _construct_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> float:
    value = _core_scalar(loader, node, "float", "a number").lower()
    # Python reads "inf", "+inf", "-inf" and "nan" but not YAML's leading dot.
    return float(value.replace(".inf", "inf").replace(".nan", "nan"))


def _construct_null(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> None:
    _core_scalar(loader, node, "null", "null (use ~, null or nothing)")


def _unsupported(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    raise _Rejected(f"unsupported tag {validate.show(_short_tag(node.tag))}", node.start_mark)


class _Loader(yaml.SafeLoader):
    """``SafeLoader`` with the YAML 1.2 core schema's scalars (so ``yes``, ``on``, ``017`` and
    ``1:30`` are not special) and tags (so no ``!!timestamp``, ``!!binary``, ``!!set``...),
    without anchors, aliases or merge keys (they enable exponential blow-up and config files
    need none), without the non-specific tag ``!``, with string keys only and no duplicate
    keys."""

    # Only the core schema's implicit types and tags (added below).
    yaml_implicit_resolvers: dict[Any, Any] = {}  # noqa: RUF012 (PyYAML's class attribute)
    yaml_constructors: dict[Any, Any] = {  # noqa: RUF012 (PyYAML's class attribute)
        f"{_TAG_PREFIX}{kind}": yaml.SafeLoader.yaml_constructors[f"{_TAG_PREFIX}{kind}"]
        for kind in ("str", "seq", "map")
    } | {None: _unsupported}

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.tagged: set[int] = set()

    def compose_node(self, parent: yaml.Node | None, index: Any) -> yaml.Node | None:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _Rejected("anchors and aliases are not allowed", event.start_mark)
        tag = getattr(event, "tag", None)
        if tag == "!":  # the non-specific tag, written ! or !<!>
            raise _Rejected("unsupported tag '!'", event.start_mark)
        node = super().compose_node(parent, index)  # pyright: ignore[reportUnknownMemberType]
        if tag is not None:
            self.tagged.add(id(node))
        return node

    def construct_document(self, node: yaml.Node) -> Any:
        _check_keys(node, self.tagged)
        return super().construct_document(node)  # pyright: ignore[reportUnknownMemberType]


for _kind, _pattern in _CORE_FORMS.items():
    _tag = f"{_TAG_PREFIX}{_kind}"
    _Loader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
        _tag, re.compile(rf"^(?:{_pattern.pattern})\Z"), list(_FIRST_CHARACTERS[_kind])
    )
_Loader.add_constructor(f"{_TAG_PREFIX}bool", _construct_bool)  # pyright: ignore[reportUnknownMemberType]
_Loader.add_constructor(f"{_TAG_PREFIX}int", _construct_int)  # pyright: ignore[reportUnknownMemberType]
_Loader.add_constructor(f"{_TAG_PREFIX}float", _construct_float)  # pyright: ignore[reportUnknownMemberType]
_Loader.add_constructor(f"{_TAG_PREFIX}null", _construct_null)  # pyright: ignore[reportUnknownMemberType]


def _yaml_error(path: Path, exc: yaml.YAMLError) -> ConfigError:
    """One line naming the file and, where PyYAML knows them, the line, column and key."""
    details: dict[str, Any] = {"file": str(path)}
    mark = None
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        text = "; ".join(part for part in (exc.context, exc.problem) if part)
    elif isinstance(exc, yaml.reader.ReaderError):
        text = (
            f"position {exc.position}: unacceptable character #x{exc.character:04x}: {exc.reason}"
        )
    else:
        text = " ".join(str(exc).split())
    position = ""
    if mark is not None:
        details.update(line=mark.line + 1, column=mark.column + 1)
        position = f"line {mark.line + 1}, column {mark.column + 1}: "
    if isinstance(exc, _Rejected) and exc.key is not None:
        details["key"] = exc.key
    return ConfigError(f"invalid config {path}: {position}{text}", **details)


def _dangling(path: Path) -> Path | None:
    """The first of ``path`` and its ancestors (from the root down) that is a dangling
    symlink, if any."""
    for candidate in (*reversed(path.parents), path):
        if candidate.is_symlink() and not candidate.exists():
            return candidate
    return None


def _cannot_read(path: Path, reason: str) -> ConfigError:
    return ConfigError(f"cannot read config {path}: {reason}", file=str(path))


# The largest config file or drop-in termctl reads.
MAX_FILE_SIZE = 1024 * 1024


def _read_bytes(path: Path) -> bytes:
    """The contents of ``path``, which must be a regular file (after symlinks) of at most
    ``MAX_FILE_SIZE`` bytes. Checked before opening (so a socket or device is never opened) and
    again after (in case it was replaced); opened without blocking or taking a controlling
    terminal, so a FIFO is never waited on. At most one byte past the cap is read."""
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise _cannot_read(path, "not a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _cannot_read(path, "not a regular file")
        with open(fd, "rb", closefd=False) as file:
            data = file.read(MAX_FILE_SIZE + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_SIZE:
        raise _cannot_read(path, f"file too large (more than {MAX_FILE_SIZE} bytes)")
    return data


def _read(path: Path, *, optional: bool = False) -> dict[str, Any]:
    """Read one config file. With ``optional``, a missing file (not a dangling symlink, nor
    below one) means no settings; every other failure is a ``ConfigError`` naming the file."""
    try:
        text = _read_bytes(path).decode("utf-8")
    except FileNotFoundError:
        if dangling := _dangling(path):
            raise _cannot_read(dangling, "dangling symlink") from None
        if optional:
            return {}
        raise _cannot_read(path, os.strerror(errno.ENOENT)) from None
    except OSError as exc:
        raise _cannot_read(path, exc.strerror or str(exc)) from None
    except UnicodeDecodeError:
        raise _cannot_read(path, "not UTF-8") from None
    try:
        loader = _Loader(text)  # checks for unprintable characters
        loader.name = str(path)
        try:
            data = loader.get_single_data()
        finally:
            loader.dispose()
    except yaml.YAMLError as exc:
        raise _yaml_error(path, exc) from None
    except RecursionError:
        raise ConfigError(f"invalid config {path}: nested too deeply", file=str(path)) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"invalid config {path}: the top level must be a mapping", file=str(path))
    return data  # pyright: ignore[reportUnknownVariableType]


def _dropins(main: Path) -> list[Path]:
    """``config.d/*.yaml`` next to the main file, sorted by code point; hidden files are
    skipped, as a shell glob would. No ``config.d`` means no drop-ins; one that cannot be
    listed (a dangling symlink included) is a ``ConfigError``."""
    directory = main.parent / "config.d"
    try:
        with os.scandir(directory) as entries:
            names = [e.name for e in entries if e.name.endswith(".yaml")]
    except FileNotFoundError:
        if dangling := _dangling(directory):
            raise _cannot_read(dangling, "dangling symlink") from None
        return []
    except OSError as exc:
        raise _cannot_read(directory, exc.strerror or str(exc)) from None
    return [directory / name for name in sorted(names) if not name.startswith(".")]


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
    if error["type"] == "bool_type" and isinstance(error["input"], str):
        message += " (use true or false)"
    return _key(error["loc"]), message


_NOT_UNICODE = "must be valid Unicode text"


def _check_unicode(data: Any, path: tuple[int | str, ...] = ()) -> None:
    """Every key and string in ``data`` is valid Unicode (no lone surrogates, which YAML's
    ``\\u`` escapes can produce); the first violation in document order is reported."""
    if isinstance(data, str):
        if validate.has_surrogate(data):
            raise _key_error(_key(path), _NOT_UNICODE)
    elif isinstance(data, dict):
        for key, value in data.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(key, str) and validate.has_surrogate(key):
                raise _key_error(_key((*path, key)), _NOT_UNICODE)
            _check_unicode(value, (*path, key))  # pyright: ignore[reportUnknownArgumentType]
    elif isinstance(data, list):
        for index, value in enumerate(data):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            _check_unicode(value, (*path, index))


def _key_error(key: str, message: str) -> ConfigError:
    """An error at ``key``; an empty or overlong key is quoted (and cut) so the message reads."""
    shown = validate.show(key) if not key or len(key) > validate.SHOW_LIMIT else key
    return ConfigError(f"invalid config: {shown}: {message}", key=key)


def _env_adapter(key: str) -> TypeAdapter[Any]:
    field = Config.model_fields[key]
    return TypeAdapter(
        Annotated[field.annotation, field],  # pyright: ignore[reportInvalidTypeForm, reportArgumentType]
        config=ConfigDict(strict=True),
    )


# One validator per TERMCTL_<KEY>, built from the Config field so the two cannot drift.
_ENV_ADAPTERS = {key: _env_adapter(key) for key in ENV_KEYS}


def _from_environment(environ: Mapping[str, str]) -> dict[str, Any]:
    """The valid ``TERMCTL_<KEY>`` values that are set, by key."""
    values: dict[str, Any] = {}
    for key in ENV_KEYS:
        variable = f"TERMCTL_{key.upper()}"
        if variable not in environ:
            continue
        if validate.has_surrogate(environ[variable]):
            raise ConfigError(f"invalid {variable}: {_NOT_UNICODE}", variable=variable)
        try:
            values[key] = _ENV_ADAPTERS[key].validate_python(environ[variable])
        except ValidationError as exc:
            message = _first_error(exc)[1]
            raise ConfigError(f"invalid {variable}: {message}", variable=variable) from None
    return values


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
    """Load the effective config: built-in defaults, the main file, the drop-ins in code point
    order, then ``TERMCTL_<KEY>`` scalars. Raises ``ConfigError`` naming the key, variable or
    file. A missing main file is an error only when given with ``--config``."""
    main = config_path(environ, explicit)
    data = merge(copy.deepcopy(DEFAULTS), _read(main, optional=explicit is None))
    for path in _dropins(main):
        data = merge(data, _read(path))

    _check_unicode(data)
    _check_pre_hooks(data)
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        raise _key_error(*_first_error(exc)) from None
    cfg = cfg.model_copy(update=_from_environment(environ))
    _check_rules(cfg)
    return cfg
