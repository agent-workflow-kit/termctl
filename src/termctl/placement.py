"""Placement and rule resolution, and the ``placements`` command.

interface.md "Placement" and "`placements`" are normative. A placement is resolved to its
*effective* fields: the built-in ``default`` with the configured ``default`` merged over it field
by field, and every other placement's missing fields taken from that effective ``default``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from .config import DEFAULTS, Config, Placement, Rule
from .errors import TermctlError, UsageError
from .output import Result
from .validate import show

DEFAULT = "default"

# The built-in default placement (interface.md "Placement").
BUILT_IN_DEFAULT: dict[str, Any] = dict(DEFAULTS["placements"][DEFAULT])

# Fields in the order they are printed.
FIELDS = ("session", "target", "max_panes", "layout", "group_by")

# Fields of `target: pane` placements, which this version does not support (interface.md
# "Placement", design.md D2).
PANE_FIELDS = ("max_panes", "layout", "group_by")

_PLAIN = re.compile(r"[A-Za-z0-9_-]+")

# Plain strings that would read as a JSON literal or number when shown bare.
_JSON_TOKEN = re.compile(r"null|true|false|-?(?:0|[1-9][0-9]*)(?:[eE][0-9]+)?")

Origin = Literal["built-in", "config"]


def _set_fields(placement: Placement) -> dict[str, Any]:
    """The fields ``placement`` sets, explicit nulls included."""
    return {name: getattr(placement, name) for name in FIELDS if name in placement.model_fields_set}


def _ordered(fields: dict[str, Any]) -> dict[str, Any]:
    return {name: fields[name] for name in FIELDS if name in fields}


def effective(cfg: Config) -> dict[str, dict[str, Any]]:
    """Every placement's effective fields, ``default`` first, then config order. Returns new
    dicts; the config is not changed."""
    configured = cfg.placements.get(DEFAULT)
    default = {**BUILT_IN_DEFAULT, **(_set_fields(configured) if configured else {})}
    result = {DEFAULT: _ordered(default)}
    for name, placement in cfg.placements.items():
        if name != DEFAULT:
            result[name] = _ordered({**default, **_set_fields(placement)})
    return result


def origin(cfg: Config, name: str) -> Origin:
    """``config`` when any field of ``name`` comes from config, else ``built-in``. A field of
    another placement comes from config when the placement sets it, or when it inherits it from
    a ``default`` field that config set."""
    own = _set_fields(cfg.placements[name]) if name != DEFAULT else {}
    return "config" if own or cfg.configured_default_fields else "built-in"


def _pair(label: str) -> tuple[str, str]:
    key, _, value = label.partition("=")
    return key, value


def match_rule(rules: Iterable[Rule], labels: Iterable[str]) -> int | None:
    """The index of the first rule whose every ``match`` pair is among ``labels`` (``key=value``
    strings, split at the first ``=``), or None. An empty ``match`` matches everything."""
    pairs = {_pair(label) for label in labels}
    for index, rule in enumerate(rules):
        if all(pair in pairs for pair in rule.match.items()):
            return index
    return None


def check_supported(name: str, fields: dict[str, Any], *, target_from_flag: bool = False) -> None:
    """Exit 2 (``not_supported``) when the placement uses ``target: pane`` or its fields
    (present at all, an explicit null included). ``target_from_flag`` says the target came
    from ``--target``, so the message names the flag rather than the placement for it."""
    pane_target = fields.get("target") == "pane"
    pane_fields = [field for field in PANE_FIELDS if field in fields]
    used = (["target"] if pane_target else []) + pane_fields
    if not used:
        return
    if pane_target and target_from_flag:
        if pane_fields:
            message = (
                f"--target pane is not supported, and placement {name!r} uses"
                f" {', '.join(pane_fields)}, which this version does not support"
                " (only target: window)"
            )
        else:
            message = f"--target pane is not supported (placement {name!r}; only target: window)"
    else:
        shown = ", ".join("target: pane" if field == "target" else field for field in used)
        message = (
            f"placement {name!r} uses {shown}, which this version does not support"
            " (only target: window)"
        )
    raise TermctlError("not_supported", message, details={"placement": name, "fields": used})


@dataclass(frozen=True)
class Selection:
    """The placement ``open`` would use: the matching rule's index (None when no rule matched or
    ``--placement`` named it), the placement's name and its effective fields after the
    ``--session``/``--target`` overrides."""

    rule: int | None
    name: str
    fields: dict[str, Any]

    @property
    def session(self) -> str:
        return self.fields["session"]

    @property
    def target(self) -> str:
        return self.fields["target"]


def select(
    cfg: Config,
    labels: Iterable[str],
    *,
    placement: str | None = None,
    session: str | None = None,
    target: str | None = None,
) -> Selection:
    """Resolve the placement for ``open`` (interface.md "Placement", resolution steps 1-3).

    ``placement`` (``--placement``) names the placement and skips the rules; otherwise the
    first matching rule's placement, else ``default``. An undefined name exits 2 (``usage``).
    ``session`` and ``target`` (``--session``, ``--target``; None when not given) then
    override its fields, and the result is checked for ``not_supported``. The arguments are
    assumed to have passed their step-1 grammar checks.
    """
    placements = effective(cfg)
    if placement is not None:
        if placement not in placements:
            raise UsageError(f"undefined placement {show(placement)}")
        rule, name = None, placement
    else:
        rule = match_rule(cfg.rules, labels)
        name = DEFAULT if rule is None else cfg.rules[rule].placement
    fields = placements[name]
    if session is not None:
        fields["session"] = session
    if target is not None:
        fields["target"] = target
    check_supported(name, fields, target_from_flag=target is not None)
    return Selection(rule=rule, name=name, fields=fields)


# --- the placements command ---------------------------------------------------------------------


def _quote(text: str) -> str:
    """``text`` in double quotes with ``"`` and ``\\`` backslash-escaped. Control characters
    are left to the output layer, which shows them as ``\\xNN``; a line break is written as
    ``\\x0a`` here, since the output layer keeps line breaks."""
    quoted = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\x0a")
    return f'"{quoted}"'


def _show_value(value: str | int | None) -> str:
    """Integers and null bare; a string bare when it is plain and cannot read as null, a
    boolean or a number, else quoted, so a line stays unambiguous."""
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if _PLAIN.fullmatch(value) and not _JSON_TOKEN.fullmatch(value):
        return value
    return _quote(value)


def _placement_line(name: str, fields: dict[str, Any], origin: Origin) -> str:
    shown = " ".join(f"{field}={_show_value(value)}" for field, value in fields.items())
    return f"placement {name}: {shown} ({origin})"


def _rule_line(index: int, rule: Rule) -> str:
    pairs = " ".join(f"{key}={value}" for key, value in rule.match.items()) or "(any)"
    return f"rule {index}: {pairs} -> {rule.placement}"


def report(cfg: Config, labels: list[str] | None) -> Result:
    """The ``placements`` output; ``labels`` (sorted, unique ``key=value`` pairs) is None
    without ``--label``."""
    placements = {
        name: {**fields, "origin": origin(cfg, name)} for name, fields in effective(cfg).items()
    }
    rules = [rule.model_dump() for rule in cfg.rules]
    lines = [
        _placement_line(name, {k: v for k, v in fields.items() if k != "origin"}, fields["origin"])
        for name, fields in placements.items()
    ]
    lines += [_rule_line(index, rule) for index, rule in enumerate(cfg.rules)]
    match = None
    if labels is not None:
        selected = select(cfg, labels)
        match = {"labels": labels, "rule": selected.rule, "placement": selected.name}
        found = "no rule" if selected.rule is None else f"rule {selected.rule}"
        lines.append(f"match {' '.join(labels)}: {found} -> {selected.name}")
    return Result(
        data={"placements": placements, "rules": rules, "match": match}, human="\n".join(lines)
    )
