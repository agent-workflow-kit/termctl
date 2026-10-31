"""Hook execution (interface.md "Hooks"; design guide section 8).

A hook's ``cmd`` runs as ``/bin/sh -c <cmd>`` (the design guide prescribes ``sh -c``; the
absolute path keeps the caller's ``PATH`` from choosing the shell) in a process group the
gateway creates for it, with the JSON payload on stdin and the caller's full environment plus
``TERMCTL_HOOK``, ``TERMCTL_ID`` and ``TERMCTL_OBJECT_TYPE``. There is no template
substitution: nothing from the payload reaches the command line or the environment.

Pre-hooks run in order: exit 0 continues; exit 13 escalates and the remaining hooks still run;
any other exit, a signal, a timeout or a failure to start vetoes and stops the rest. A run with
``--override-escalation`` treats exit 13 as allow; a veto still vetoes. Post-hooks cannot undo
anything: their failures become warnings, never errors. An async post-hook runs under a
detached watchdog (``hookwatch``) that enforces its timeout.

Hook output is untrusted. termctl keeps at most ``STDOUT_LIMIT`` bytes of a pre-hook's stdout
(read for ``decision_id``) and ``STDERR_LIMIT`` bytes of its stderr (reported as
``hook_stderr``), reads and discards the rest, and validates every decision id against the
grammar. Hook output never enters a message or a warning's ``problem``; ``hook_stderr`` is
printed on lines of its own (``Output.hook_stderr``), escaped like all human output.

No hook runs while this process holds a server lock (``lock.held()``): hooks may run long, and
the lock must never wait on them.
"""

# open (cli.open_pane) and close (cli.close_pane) build their payloads here, run the pre-hooks
# (open order step 4, close step 2) and the post-hooks, print and log the warnings
# (``HookWarning.log_record``), list the hook process groups in their log records
# (``HookProcess.log_entry``; design guide section 8: the group is "created and recorded")
# and audit override runs.

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NoReturn

from . import jsonlog, lock, validate
from .config import DEFAULT_HOOK_TIMEOUT_S, Config, PostHook, PreHook
from .errors import TermctlError, UsageError
from .gateway import Gateway, GroupNotStopped, GroupRun
from .ids import is_id
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
    """A hook problem that is not a verdict: a failed post-hook or unusable decision ids.
    Callers print ``message`` as a warning, ``hook_stderr`` separately (``Output.hook_stderr``)
    and log ``log_record`` (``hook_failed``).

    ``problem`` is termctl's own text and never holds hook output; ``hook_stderr`` is the
    hook's stderr (**untrusted**). ``pgid`` is the process group termctl created for the hook
    (for an async hook, its watchdog's), None if it could not be started.
    """

    hook: str
    cmd: str
    problem: str
    pgid: int | None = None
    hook_stderr: str = ""

    @property
    def message(self) -> str:
        return f"{self.hook} hook {validate.show(self.cmd)}: {self.problem}"

    def log_record(
        self, ts: datetime, *, id: str | None, socket_path: str | None
    ) -> dict[str, Any]:
        details: dict[str, Any] = {"hook": self.hook, "cmd": self.cmd, "problem": self.problem}
        if self.pgid is not None:
            details["pgid"] = self.pgid
        if self.hook_stderr:
            details["hook_stderr"] = self.hook_stderr
        return jsonlog.log_record(
            ts,
            event="hook_failed",
            result="warning",
            id=id,
            socket_path=socket_path,
            message=self.message,
            details=details,
        )


@dataclass(frozen=True)
class HookProcess:
    """A hook that was started: ``pgid`` is the process group termctl created for it, or for
    an async hook (``watchdog``) the watchdog's group, which is also its session."""

    hook: str
    cmd: str
    pgid: int
    watchdog: bool = False

    def log_entry(self) -> dict[str, Any]:
        """Its entry in the ``hooks`` list of the step's log record (``schemas/log.json``)."""
        return {"hook": self.hook, "cmd": self.cmd, "pgid": self.pgid, "async": self.watchdog}


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
            raise _BadJson("duplicate key")  # the key itself is hook output: not echoed
        result[key] = value
    return result


def _no_constant(name: str) -> NoReturn:
    raise _BadJson("NaN or Infinity")


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


def _drop_reason(item: Any, ids: list[str]) -> str | None:
    """Why the decision id ``item`` is dropped, without echoing it; None to keep it."""
    if not isinstance(item, str):
        return f"is not a string ({type(item).__name__})"
    if not validate.is_decision_id(item):
        return "does not match [A-Za-z0-9._:@/-]{1,128}"
    if len(ids) >= validate.MAX_DECISION_IDS:
        return f"is beyond the limit of {validate.MAX_DECISION_IDS} in a list"
    return None


def _collect_ids(hook: Hook, run: GroupRun, ids: list[str], warnings: list[HookWarning]) -> None:
    """Append the valid decision ids of ``run``'s stdout to ``ids`` (at most 32 in all) and at
    most one warning for the hook, which counts the ids dropped and names the first problem.
    No warning echoes hook output."""

    def warn(problem: str) -> None:
        warnings.append(HookWarning(hook.name, hook.cmd, problem, pgid=run.pgid))

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
    dropped = 0
    first = ""
    for index, item in enumerate(items, start=1):
        reason = _drop_reason(item, ids)
        if reason is None:
            ids.append(item)
            continue
        dropped += 1
        if dropped == 1:
            first = f"decision id #{index} {reason}"
    if dropped:
        noun = "decision id" if dropped == 1 else "decision ids"
        warn(f"dropped {dropped} {noun}; the first: {first}")


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
    # TERMCTL_ID comes from object_id, so it must be a termctl id and nothing else.
    if not is_id(payload.get("object_id")):
        raise ValueError("the payload's object_id is not a termctl id")
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


def _start_problem(exc: OSError | ValueError) -> str:
    """Why a hook could not be started (an ``OSError``, or a ``ValueError`` such as a NUL in
    the command), in the system's words."""
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc)


@dataclass
class PreHookOutcome:
    """The verdict of a run of pre-hooks.

    ``decision_ids`` is the ``--decision-id`` values followed by the ids hooks returned.
    ``hook`` and ``hook_stderr`` belong to the hook that vetoed, else to the first one that
    escalated (and was not overridden). ``message`` is termctl's own text; it never holds hook
    output. ``overridden`` lists the hooks whose escalation an ``--override-escalation`` run
    treated as allow. ``processes`` lists every hook started, with its process group.
    """

    verdict: Verdict
    decision_ids: list[str]
    hook: str | None = None
    hook_stderr: str | None = None
    message: str | None = None
    overridden: list[str] = field(default_factory=list[str])
    warnings: list[HookWarning] = field(default_factory=list[HookWarning])
    processes: list[HookProcess] = field(default_factory=list[HookProcess])

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

    def _decide(self, verdict: Verdict, hook: Hook, stderr: str, message: str) -> None:
        self.verdict, self.hook, self.hook_stderr, self.message = (
            verdict,
            hook.cmd,
            stderr,
            message,
        )


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
        except (OSError, ValueError) as exc:
            message = (
                f"{name} hook could not be started ({_start_problem(exc)}), "
                f"counted as a veto: {hook.cmd}"
            )
            outcome._decide("vetoed", hook, "", message)
            return outcome
        except GroupNotStopped as exc:
            outcome.processes.append(HookProcess(hook.name, hook.cmd, exc.pgid))
            message = (
                f"{name} hook timed out after {hook.timeout} s and could not be stopped, "
                f"counted as a veto: {hook.cmd}"
            )
            outcome._decide("vetoed", hook, "", message)
            return outcome
        outcome.processes.append(HookProcess(hook.name, hook.cmd, run.pgid))
        stderr = cut_stderr(run.stderr)
        if run.returncode is None:
            message = f"{name} hook timed out after {hook.timeout} s, counted as a veto: {hook.cmd}"
            outcome._decide("vetoed", hook, stderr, message)
            return outcome
        _collect_ids(hook, run, outcome.decision_ids, outcome.warnings)
        if run.returncode == 0:
            continue
        if run.returncode == ESCALATE_EXIT:
            if override_escalation:
                outcome.overridden.append(hook.cmd)
            elif outcome.verdict == "allow":
                message = f"{name} hook escalated (exit 13): {hook.cmd}"
                outcome._decide("escalated", hook, stderr, message)
            continue
        how = (
            f"killed by signal {-run.returncode}"
            if run.returncode < 0
            else f"exit {run.returncode}"
        )
        outcome._decide("vetoed", hook, stderr, f"{name} hook vetoed ({how}): {hook.cmd}")
        return outcome
    return outcome


@dataclass
class PostHookOutcome:
    """A run of post-hooks: a warning for each that failed, and every hook started with its
    process group (for an async hook, its watchdog's)."""

    warnings: list[HookWarning] = field(default_factory=list[HookWarning])
    processes: list[HookProcess] = field(default_factory=list[HookProcess])


def run_post_hooks(
    hooks: Sequence[Hook],
    payload: Mapping[str, Any],
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
) -> PostHookOutcome:
    """Run ``hooks`` (all named ``payload["hook"]``) in order; return a warning for each that
    failed. An async hook is started detached under a watchdog that enforces its timeout, and
    is not waited for. Raises like ``run_pre_hooks``."""
    _check_payload(hooks, payload, POST_HOOKS)
    data = _encode(payload)
    env = _environment(environ, payload)
    outcome = PostHookOutcome()

    def warn(hook: Hook, problem: str, pgid: int | None, stderr: str = "") -> None:
        outcome.warnings.append(HookWarning(hook.name, hook.cmd, problem, pgid, stderr))

    for hook in hooks:
        argv = [SHELL, "-c", hook.cmd]
        try:
            if hook.async_:
                pgid = gateway.spawn_watched(argv, stdin=data, env=env, timeout=hook.timeout)
                outcome.processes.append(HookProcess(hook.name, hook.cmd, pgid, watchdog=True))
                continue
            run = gateway.run_group(
                argv,
                stdin=data,
                env=env,
                timeout=hook.timeout,
                stdout_limit=0,
                stderr_limit=STDERR_LIMIT,
            )
        except (OSError, ValueError) as exc:
            warn(hook, f"could not be started: {_start_problem(exc)}", None)
            continue
        except GroupNotStopped as exc:
            outcome.processes.append(HookProcess(hook.name, hook.cmd, exc.pgid))
            problem = f"timed out after {hook.timeout} s; its process group could not be stopped"
            warn(hook, problem, exc.pgid)
            continue
        outcome.processes.append(HookProcess(hook.name, hook.cmd, run.pgid))
        stderr = cut_stderr(run.stderr)
        if run.returncode is None:
            problem = f"timed out after {hook.timeout} s; its process group was killed"
            warn(hook, problem, run.pgid, stderr)
        elif run.returncode < 0:
            warn(hook, f"killed by signal {-run.returncode}", run.pgid, stderr)
        elif run.returncode != 0:
            warn(hook, f"exited {run.returncode}", run.pgid, stderr)
    return outcome
