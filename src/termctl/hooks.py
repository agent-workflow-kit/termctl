"""Hook execution (interface.md "Hooks"; design guide section 8).

A hook's ``cmd`` runs as ``/bin/sh -c <cmd>`` (the design guide prescribes ``sh -c``; the
absolute path keeps the caller's ``PATH`` from choosing the shell) in a process group the
gateway creates for it, with the JSON payload on stdin and the caller's full environment plus
``TERMCTL_HOOK``, ``TERMCTL_ID`` and ``TERMCTL_OBJECT_TYPE``. There is no template
substitution: nothing from the payload reaches the command line or the environment.

Pre-hooks run in order: exit 0 continues; exit 13 escalates and the remaining hooks still run;
any other exit, a signal, a timeout or a failure to start vetoes and stops the rest. A run with
``--override-escalation`` treats exit 13 as allow; a veto still vetoes. Post-hooks cannot undo
anything: their failures become warnings, never errors.

Hook output is untrusted. termctl keeps at most ``STDOUT_LIMIT`` bytes of a pre-hook's stdout
(read for ``decision_id``) and ``STDERR_LIMIT`` bytes of its stderr (reported as
``hook_stderr``), reads and discards the rest, and validates every decision id against the
grammar; output is escaped wherever it is printed (``output.escape``).

No hook runs while this process holds a server lock (``lock.held()``): hooks may run long, and
the lock must never wait on them.
"""

# TODO(#17): open builds its pre-open/post-open payloads here, runs the hooks at steps 4 and 9
# of the open order, prints and logs the warnings (``HookWarning.log_record``) and audits
# override runs. TODO(#23): the same for close (pre-close at step 2, post-close).

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NoReturn

from . import jsonlog, lock, validate
from .config import DEFAULT_HOOK_TIMEOUT_S, Config, PostHook, PreHook
from .errors import TermctlError, UsageError
from .gateway import Gateway, GroupRun
from .output import to_json

SHELL = "/bin/sh"
TOOL = "termctl"
OBJECT_TYPE = "pane"
ESCALATE_EXIT = 13
# `--hook` hooks have this fixed timeout and are never async (interface.md "Hooks").
FLAG_HOOK_TIMEOUT_S = DEFAULT_HOOK_TIMEOUT_S
# Enough for 32 decision ids of 128 characters in any reasonable JSON object.
STDOUT_LIMIT = 65536
# hook_stderr is cut to 4096 bytes (interface.md "Errors").
STDERR_LIMIT = 4096

COMMAND_HOOKS: dict[str, tuple[str, ...]] = {
    "open": ("pre-open", "post-open"),
    "close": ("pre-close", "post-close"),
}
PRE_HOOKS = ("pre-open", "pre-close")
POST_HOOKS = ("post-open", "post-close")

Verdict = Literal["allow", "vetoed", "escalated"]


@dataclass(frozen=True)
class Hook:
    """One hook to run: from config ``hooks`` or from ``--hook``."""

    name: str
    cmd: str
    timeout: int
    async_: bool = False


# --- which hooks run ----------------------------------------------------------------------------


def parse_flag_hooks(command: str, values: Sequence[str]) -> list[Hook]:
    """``--hook NAME=CMD`` values, in order. NAME must be one of ``command``'s hooks and CMD
    non-empty valid Unicode text; anything else is a usage error (exit 2)."""
    allowed = COMMAND_HOOKS[command]
    parsed: list[Hook] = []
    for value in values:
        name, sep, cmd = value.partition("=")
        if not sep:
            raise UsageError(f"invalid --hook {validate.show(value)}: expected NAME=CMD")
        if name not in allowed:
            raise UsageError(
                f"invalid --hook name {validate.show(name)}: {command} takes "
                + " or ".join(allowed)
            )
        if not cmd:
            raise UsageError(f"invalid --hook {validate.show(value)}: CMD must not be empty")
        if validate.has_surrogate(cmd):
            raise UsageError(
                f"invalid --hook {validate.show(value)}: CMD must be valid Unicode text"
            )
        parsed.append(Hook(name, cmd, FLAG_HOOK_TIMEOUT_S))
    return parsed


def _configured(config: Config, name: str) -> list[PreHook] | list[PostHook]:
    hooks = config.hooks
    return {
        "pre-open": hooks.pre_open,
        "pre-close": hooks.pre_close,
        "post-open": hooks.post_open,
        "post-close": hooks.post_close,
    }[name]


def hooks_for(config: Config, name: str, flag_hooks: Sequence[Hook]) -> list[Hook]:
    """The hooks named ``name``: the configured ones, then the ``--hook`` ones."""
    configured = [
        Hook(name, entry.cmd, entry.timeout, isinstance(entry, PostHook) and entry.async_)
        for entry in _configured(config, name)
    ]
    return configured + [hook for hook in flag_hooks if hook.name == name]


def check_override_gate(override: bool, *, actor: str | None, stdin_is_tty: bool) -> None:
    """``--override-escalation`` is accepted only when stdin is a TTY or with ``--actor
    human:<user>`` (exit 2 otherwise). Advisory: see interface.md "Residual risks"."""
    if not override or stdin_is_tty:
        return
    if actor is not None and actor.startswith("human:"):
        return
    raise UsageError(
        "--override-escalation needs a terminal on stdin or --actor human:<user>; "
        "automation never overrides an escalation on its own"
    )


# --- payloads -----------------------------------------------------------------------------------


def _common(hook: str, object_id: str, labels: Sequence[str]) -> dict[str, Any]:
    return {
        "tool": TOOL,
        "hook": hook,
        "object_type": OBJECT_TYPE,
        "object_id": object_id,
        "labels": list(labels),
    }


def pre_open_payload(
    *,
    object_id: str,
    labels: Sequence[str],
    name: str,
    cwd: str,
    argv: Sequence[str],
    env_names: Sequence[str],
    placement: str,
    session: str,
    target: str,
    socket_path: str,
    keep_on_exit: str,
    focus: bool,
    actor: str,
    decision_ids: Sequence[str],
    override_escalation: bool,
) -> dict[str, Any]:
    """``schemas/hooks/pre-open.json``. ``env_names`` are names only, never values."""
    return {
        **_common("pre-open", object_id, labels),
        "name": name,
        "cwd": cwd,
        "argv": list(argv),
        "env_names": list(env_names),
        "placement": placement,
        "session": session,
        "target": target,
        "socket_path": socket_path,
        "keep_on_exit": keep_on_exit,
        "focus": focus,
        "actor": actor,
        "decision_ids": list(decision_ids),
        "override_escalation": override_escalation,
    }


def pre_close_payload(
    *,
    pane: Mapping[str, Any],
    force: bool,
    grace_ms: int,
    actor: str,
    decision_ids: Sequence[str],
    override_escalation: bool,
) -> dict[str, Any]:
    """``schemas/hooks/pre-close.json``: ``pane`` is the pane object read at step 1."""
    return {
        **_common("pre-close", pane["id"], pane["labels"]),
        "pane": dict(pane),
        "force": force,
        "grace_ms": grace_ms,
        "actor": actor,
        "decision_ids": list(decision_ids),
        "override_escalation": override_escalation,
    }


def post_open_payload(*, pane: Mapping[str, Any]) -> dict[str, Any]:
    """``schemas/hooks/post-open.json``: ``pane`` is the ``open`` output."""
    return {**_common("post-open", pane["id"], pane["labels"]), "pane": dict(pane)}


def post_close_payload(*, result: Mapping[str, Any], labels: Sequence[str]) -> dict[str, Any]:
    """``schemas/hooks/post-close.json``: ``result`` is the ``close`` output; ``labels`` are
    the pane's labels as read at step 1."""
    return {**_common("post-close", result["id"], labels), "result": dict(result)}


# --- warnings -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookWarning:
    """A hook problem that is not a verdict: a failed post-hook or an unusable decision id.
    Callers print ``message`` as a warning and log ``log_record`` (``hook_failed``)."""

    hook: str
    cmd: str
    problem: str

    @property
    def message(self) -> str:
        return f"{self.hook} hook {validate.show(self.cmd)}: {self.problem}"

    def log_record(
        self, ts: datetime, *, id: str | None, socket_path: str | None
    ) -> dict[str, Any]:
        return jsonlog.log_record(
            ts,
            event="hook_failed",
            result="warning",
            id=id,
            socket_path=socket_path,
            message=self.message,
            details={"hook": self.hook, "cmd": self.cmd, "problem": self.problem},
        )


# --- untrusted output ---------------------------------------------------------------------------


def cut_stderr(data: bytes, limit: int = STDERR_LIMIT) -> str:
    """At most ``limit`` bytes of ``data``, cut at a UTF-8 character boundary, as text; bytes
    that are not UTF-8 become U+FFFD."""
    data = data[:limit]
    # Drop a multi-byte character that the cut (or the hook) left incomplete at the end.
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:  # a continuation byte: keep looking for the lead byte
            continue
        if byte >= 0xF8:  # never part of UTF-8: decoded as U+FFFD
            need = 1
        elif byte >= 0xF0:
            need = 4
        elif byte >= 0xE0:
            need = 3
        elif byte >= 0xC0:
            need = 2
        else:
            need = 1
        if need > back:
            data = data[:-back]
        break
    return data.decode("utf-8", errors="replace")


class _BadJson(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _BadJson(f"duplicate key {validate.show(key)}")
        result[key] = value
    return result


def _no_constant(name: str) -> NoReturn:
    raise _BadJson(f"{name} is not JSON")


def _parse_stdout(run: GroupRun) -> tuple[Any, str | None]:
    """The ``decision_id`` value of a pre-hook's stdout (None when absent) and a problem."""
    if run.stdout_truncated:
        return None, f"stdout is longer than {STDOUT_LIMIT} bytes; ignored"
    if not run.stdout.strip():
        return None, None
    try:
        text = run.stdout.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, _BadJson) else "not valid JSON"
        return None, f"stdout is not a JSON object ({reason}); ignored"
    if not isinstance(data, dict):
        return None, "stdout is not a JSON object; ignored"
    return data.get("decision_id"), None  # pyright: ignore[reportUnknownMemberType]


def _collect_ids(hook: Hook, run: GroupRun, ids: list[str], warnings: list[HookWarning]) -> None:
    """Append the valid decision ids of ``run``'s stdout to ``ids`` (at most 32 in all) and a
    warning for each one dropped."""

    def warn(problem: str) -> None:
        warnings.append(HookWarning(hook.name, hook.cmd, problem))

    value, problem = _parse_stdout(run)
    if problem is not None:
        warn(problem)
        return
    if value is None:
        return
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list):
        items = list(value)  # pyright: ignore[reportUnknownArgumentType]
    else:
        warn(f"decision_id must be a string or a list, not {type(value).__name__}; ignored")
        return
    for item in items:
        if not isinstance(item, str):
            warn(f"decision id of type {type(item).__name__} dropped")
        elif not validate.is_decision_id(item):
            warn(
                f"decision id {validate.show(item)} dropped: must match [A-Za-z0-9._:@/-]{{1,128}}"
            )
        elif len(ids) >= validate.MAX_DECISION_IDS:
            warn(
                f"decision id {validate.show(item)} dropped: "
                f"a list holds at most {validate.MAX_DECISION_IDS}"
            )
        else:
            ids.append(item)


# --- running hooks ------------------------------------------------------------------------------


def _check_payload(
    hooks: Sequence[Hook], payload: Mapping[str, Any], kinds: tuple[str, ...]
) -> None:
    name = payload.get("hook")
    if name not in kinds:
        raise ValueError(f"not a payload for these hooks: {name!r}")
    for hook in hooks:
        if hook.name != name:
            raise ValueError(f"{hook.name} hook given a {name} payload")
    if lock.held():
        raise RuntimeError("hooks never run while the server lock is held")


def _environment(environ: Mapping[str, str], payload: Mapping[str, Any]) -> dict[str, str]:
    """The caller's full environment plus the hook variables, which override the caller's."""
    return {
        **environ,
        "TERMCTL_HOOK": payload["hook"],
        "TERMCTL_ID": payload["object_id"],
        "TERMCTL_OBJECT_TYPE": OBJECT_TYPE,
    }


def _encode(payload: Mapping[str, Any]) -> bytes:
    # to_json maps lone surrogates and escapes every non-ASCII character.
    return to_json(dict(payload), allow_nan=False).encode("ascii")


@dataclass
class PreHookOutcome:
    """The verdict of a run of pre-hooks.

    ``decision_ids`` is the ``--decision-id`` values followed by the ids hooks returned.
    ``hook`` and ``hook_stderr`` belong to the hook that vetoed, else to the first one that
    escalated (and was not overridden). ``overridden`` lists the hooks whose escalation an
    ``--override-escalation`` run treated as allow.
    """

    verdict: Verdict
    decision_ids: list[str]
    hook: str | None = None
    hook_stderr: str | None = None
    message: str | None = None
    overridden: list[str] = field(default_factory=list[str])
    warnings: list[HookWarning] = field(default_factory=list[HookWarning])

    def check(self) -> None:
        """Raise the error of a veto (exit 11) or an escalation (exit 13)."""
        if self.verdict == "allow":
            return
        assert self.message is not None
        raise TermctlError(
            self.verdict,
            self.message,
            hook=self.hook,
            hook_stderr=self.hook_stderr or "",
            decision_ids=list(self.decision_ids),
        )


def _with_stderr(message: str, stderr: str) -> str:
    stderr = stderr.strip()
    return f"{message}: {stderr}" if stderr else message


def run_pre_hooks(
    hooks: Sequence[Hook],
    payload: Mapping[str, Any],
    *,
    decision_ids: Sequence[str],
    override_escalation: bool,
    gateway: Gateway,
    environ: Mapping[str, str],
) -> PreHookOutcome:
    """Run ``hooks`` (all named ``payload["hook"]``) in order and return the verdict.

    Raises ``RuntimeError`` while a server lock is held and ``ValueError`` when the payload
    does not fit the hooks; both are programming errors and no hook runs.
    """
    _check_payload(hooks, payload, PRE_HOOKS)
    if payload["override_escalation"] is not override_escalation:
        raise ValueError("the payload's override_escalation must equal the run's")
    data = _encode(payload)
    env = _environment(environ, payload)
    outcome = PreHookOutcome("allow", list(decision_ids))
    name = payload["hook"]
    for hook in hooks:
        try:
            run = gateway.run_group(
                [SHELL, "-c", hook.cmd],
                stdin=data,
                env=env,
                timeout=hook.timeout,
                stdout_limit=STDOUT_LIMIT,
                stderr_limit=STDERR_LIMIT,
            )
        except OSError as exc:
            reason = exc.strerror or str(exc)
            outcome.verdict, outcome.hook, outcome.hook_stderr = "vetoed", hook.cmd, ""
            outcome.message = (
                f"{name} hook could not be started ({reason}), counted as a veto: {hook.cmd}"
            )
            return outcome
        stderr = cut_stderr(run.stderr)
        if run.returncode is None:
            outcome.verdict, outcome.hook, outcome.hook_stderr = "vetoed", hook.cmd, stderr
            outcome.message = _with_stderr(
                f"{name} hook timed out after {hook.timeout} s, counted as a veto: {hook.cmd}",
                stderr,
            )
            return outcome
        _collect_ids(hook, run, outcome.decision_ids, outcome.warnings)
        if run.returncode == 0:
            continue
        if run.returncode == ESCALATE_EXIT:
            if override_escalation:
                outcome.overridden.append(hook.cmd)
            elif outcome.verdict == "allow":
                outcome.verdict, outcome.hook, outcome.hook_stderr = "escalated", hook.cmd, stderr
                outcome.message = _with_stderr(
                    f"{name} hook escalated (exit 13): {hook.cmd}", stderr
                )
            continue
        how = (
            f"killed by signal {-run.returncode}"
            if run.returncode < 0
            else f"exit {run.returncode}"
        )
        outcome.verdict, outcome.hook, outcome.hook_stderr = "vetoed", hook.cmd, stderr
        outcome.message = _with_stderr(f"{name} hook vetoed ({how}): {hook.cmd}", stderr)
        return outcome
    return outcome


def run_post_hooks(
    hooks: Sequence[Hook],
    payload: Mapping[str, Any],
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
) -> list[HookWarning]:
    """Run ``hooks`` (all named ``payload["hook"]``) in order; return a warning for each that
    failed. An async hook is started detached and not waited for, so its timeout cannot be
    enforced once termctl has exited. Raises like ``run_pre_hooks``."""
    _check_payload(hooks, payload, POST_HOOKS)
    data = _encode(payload)
    env = _environment(environ, payload)
    warnings: list[HookWarning] = []
    for hook in hooks:
        argv = [SHELL, "-c", hook.cmd]
        try:
            if hook.async_:
                gateway.spawn_group(argv, stdin=data, env=env)
                continue
            run = gateway.run_group(
                argv,
                stdin=data,
                env=env,
                timeout=hook.timeout,
                stdout_limit=0,
                stderr_limit=STDERR_LIMIT,
            )
        except OSError as exc:
            problem = f"could not be started: {exc.strerror or exc}"
            warnings.append(HookWarning(hook.name, hook.cmd, problem))
            continue
        if run.returncode is None:
            problem = f"timed out after {hook.timeout} s; its process group was killed"
        elif run.returncode < 0:
            problem = _with_stderr(f"killed by signal {-run.returncode}", cut_stderr(run.stderr))
        elif run.returncode != 0:
            problem = _with_stderr(f"exited {run.returncode}", cut_stderr(run.stderr))
        else:
            continue
        warnings.append(HookWarning(hook.name, hook.cmd, problem))
    return warnings
