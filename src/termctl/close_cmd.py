"""``close``'s own steps (interface.md `close`, "Liveness"; design.md D7, D22, D34).

``cli.close_pane`` runs the close order: the check of step 1 (``refuse_running``,
``row_mismatch``, and for a forced running pane ``probe``), the ``pre-close`` hooks, the
re-check of step 3 (``recheck``), and steps 4 and 5 (``close``); then the ``post-close``
hooks, the log and audit records and the output. ``dry_run`` is the check of step 1 alone.

Only the managed pane is killed (``kill-pane``, never ``kill-window``: spike D5), always
inside the ownership guard of interface.md rule 5. A process is signaled only when every
check holds: the recorded ``@termctl_pid`` equaled the row's ``#{pane_pid}`` at steps 1 and 3
(and at kill time), the server answering on the socket is ``#{pid}`` as termctl's PID
namespace sees it (``SO_PEERCRED``), the process's parent is that server, and its start time
is ``@termctl_pid_start`` before every signal (``procs.verify``, ``procs.signal_sequence``).
A recorded PID that fails a check, or whose check cannot be made, is ``pid_mismatch: true``
and is never signaled.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from . import procs
from . import tmux_backend as tb
from .backend import Backend
from .config import Config
from .errors import TermctlError, UsageError
from .gateway import Clock, Gateway, ProcessHandle
from .panes import ManagedPane, PaneRead
from .procs import Identity, Verification
from .validate import parse_duration

Warn = Callable[[str], None]

SIGNALS = ["SIGTERM", "SIGKILL"]


# --- step 1 -------------------------------------------------------------------------------------


def check_force_gate(force: bool, *, actor: str | None, stdin_is_tty: bool) -> None:
    """``--force`` is accepted only when stdin is a TTY or with ``--actor human:<user>``
    (exit 2 otherwise). Advisory, like ``--override-escalation`` (interface.md "Residual
    risks")."""
    if not force or stdin_is_tty:
        return
    if actor is not None and actor.startswith("human:"):
        return
    raise UsageError(
        "close --force needs a terminal on stdin or --actor human:<user>; "
        "automation never forces a close"
    )


def grace_ms(grace: str | None, config: Config) -> int:
    """``--grace``, else config ``close_grace``, in milliseconds."""
    return parse_duration(grace if grace is not None else config.close_grace)


def refuse_running(pane: ManagedPane) -> TermctlError:
    """Exit 20, ``reason: running``: a running pane without ``--force``."""
    return TermctlError(
        "refused",
        f"managed pane {pane.id} is running; a human may close it with --force",
        reason="running",
    )


def row_mismatch(pane: ManagedPane) -> bool:
    """A recorded PID that does not equal ``#{pane_pid}`` in the same ``list-panes`` row: it
    never counts (interface.md "Liveness"). No recorded PID is no mismatch."""
    return pane.pid is not None and pane.pid != pane.pane_pid


@dataclass(frozen=True)
class Probe:
    """What ``probe`` found: the verification of the recorded PID, whether the pane was gone
    when termctl asked tmux, and the server's ``#{pid}`` tmux reported (None if unknown)."""

    verification: Verification
    pane_gone: bool = False
    server_pid: int | None = None


def probe(backend: Backend | None, gateway: Gateway, socket_path: str, pane: ManagedPane) -> Probe:
    """Prove that the pane's recorded PID is its live process (``procs.verify``), reading the
    pane's ``#{pane_pid} #{pane_dead} #{pid}`` with a guarded ``display`` first.

    ``backend`` is None when the socket checks found no server. A pane that is gone (the
    guard did not match, tmux reports it or the server gone) is ``pane_gone``; a pane that
    is now dead has no process to signal (``GONE``). A ``#{pane_pid}`` other than the
    recorded PID is ``MISMATCH``. Raises ``server_unavailable`` (exit 12) for any other tmux
    failure.
    """
    assert pane.pid is not None and pane.pid_start is not None
    if backend is None:
        return Probe(Verification(Identity.UNCHECKABLE), pane_gone=True)
    outcome, result, answer = backend.query_process(pane.pane_id, pane.id)
    if _gone(outcome, result, socket_path):
        return Probe(Verification(Identity.UNCHECKABLE), pane_gone=True)
    if outcome is tb.GuardOutcome.FAILED:
        raise tb.tmux_failure(result, pane.id, socket_path)
    if answer is None:
        problem = f"tmux gave an unexpected answer for pane {pane.pane_id}"
        return Probe(Verification(Identity.UNCHECKABLE, problem))
    if answer.dead:
        return Probe(Verification(Identity.GONE), server_pid=answer.server_pid)
    if answer.pane_pid != pane.pid:
        return Probe(Verification(Identity.MISMATCH), server_pid=answer.server_pid)
    verification = procs.verify(gateway, socket_path, pane.pid, pane.pid_start, answer.server_pid)
    return Probe(verification, server_pid=answer.server_pid)


def _gone(
    outcome: tb.GuardOutcome, result: subprocess.CompletedProcess[str], socket_path: str
) -> bool:
    """The pane is gone: the guard did not match, or tmux says the target or the server is
    gone (interface.md "Panes that vanish": ``close`` counts it as closed)."""
    if outcome is tb.GuardOutcome.NOMATCH:
        return True
    return outcome is tb.GuardOutcome.FAILED and (
        tb.is_gone(result) or tb.is_no_server(result, socket_path)
    )


# --- step 3 -------------------------------------------------------------------------------------


def recheck(read: PaneRead, first: ManagedPane, warn: Warn) -> ManagedPane | None:
    """Step 3: the pane resolved again by its full id; None when it is gone (no server
    included). Raises ``state_changed`` (exit 12) when its state differs from step 1, or when
    the id now names another tmux pane while step 1's tmux pane is still a managed pane (it
    is not the pane the hooks approved), and ``ambiguous`` (exit 21) when two panes carry the
    id. When step 1's tmux pane is no longer a managed pane, the approved pane is gone and the
    other one is left alone, with a warning."""
    matches = [pane for pane in read.panes if pane.id == first.id] if read.server_running else []
    if not matches:
        return None
    if len(matches) > 1:
        raise TermctlError(
            "ambiguous",
            f"id {first.id} matches {len(matches)} managed panes; acting on none",
            candidates=sorted(
                ({"id": p.id, "pane_id": p.pane_id, "session": p.session} for p in matches),
                key=lambda c: int(c["pane_id"][1:]),
            ),
        )
    current = matches[0]
    if current.pane_id != first.pane_id:
        if not any(pane.pane_id == first.pane_id for pane in read.panes):
            warn(
                f"managed pane {first.id} (tmux pane {first.pane_id}) is gone; its id now"
                f" appears on tmux pane {current.pane_id}, which was not touched"
            )
            return None
        raise TermctlError(
            "state_changed",
            f"managed pane {first.id} is now tmux pane {current.pane_id}, not"
            f" {first.pane_id}; nothing was closed",
        )
    if current.state != first.state:
        raise TermctlError(
            "state_changed",
            f"managed pane {first.id} was {first.state} and is now {current.state};"
            " nothing was closed",
        )
    return current


def recheck_mismatch(first: ManagedPane, current: ManagedPane) -> bool:
    """The PID check of step 1 applied to the row of step 3; a recorded PID or start time
    that changed between the two reads is a mismatch too."""
    return row_mismatch(current) or (current.pid, current.pid_start) != (
        first.pid,
        first.pid_start,
    )


# --- steps 4 and 5 ------------------------------------------------------------------------------


@dataclass
class Closing:
    """What steps 1 to 3 established. ``current`` is the pane as step 3 read it, None when it
    was gone; ``pid_mismatch`` holds for a recorded PID that failed a check of step 1 or 3;
    ``proof`` is step 1's ``probe`` of a forced running pane's recorded PID (None when none
    was made). ``signals_sent`` receives each signal as it is sent."""

    first: ManagedPane
    current: ManagedPane | None
    force: bool
    grace_s: float
    pid_mismatch: bool
    proof: Probe | None
    signals_sent: list[str] = field(default_factory=list[str])


@dataclass
class Closed:
    """The ``close`` output fields of steps 4 and 5."""

    signals_sent: list[str] = field(default_factory=list[str])
    exited: bool | None = True
    pid_mismatch: bool = False

    @classmethod
    def mismatch(cls) -> Closed:
        return cls(exited=None, pid_mismatch=True)


def close(
    backend: Backend | None,
    gateway: Gateway,
    clock: Clock,
    socket_path: str,
    closing: Closing,
    warn: Warn,
) -> Closed:
    """Steps 4 and 5, or step 3's signal sequence for a pane found gone. The pane counts as
    closed whenever this returns. Raises ``server_unavailable`` (exit 12) when tmux fails
    otherwise than by the pane or server being gone."""
    first = closing.first
    signal_target = (
        not first.dead and closing.force and first.pid is not None and not closing.pid_mismatch
    )
    if not signal_target:
        if closing.current is not None:
            _kill(backend, socket_path, closing.current, query=False)
        return Closed.mismatch() if closing.pid_mismatch else Closed()
    assert first.pid is not None
    if closing.proof is not None and closing.proof.pane_gone:
        # Gone at step 1's probe, so never proven: never signaled (``gone_at_probe``).
        if closing.current is not None:
            _kill(backend, socket_path, closing.current, query=False)
        return Closed() if gone_at_probe(gateway, first.pid, warn) else Closed.mismatch()
    if closing.current is None:
        return _signal_gone_pane(gateway, clock, closing, warn)
    return _kill_and_signal(backend, gateway, clock, socket_path, closing, warn)


def gone_at_probe(gateway: Gateway, pid: int, warn: Warn) -> bool:
    """For a pane gone at step 1's probe, which therefore proved nothing: whether its recorded
    process has exited (no such process, or a zombie, as its handle shows). Otherwise, or
    when that cannot be told, a warning: the process cannot be checked and is not signaled
    (``pid_mismatch``)."""
    try:
        handle = gateway.open_process(pid)
    except OSError:
        handle, exited = None, False
    else:
        exited = handle is None
    if handle is not None:
        try:
            exited = handle.exited() or _exited(gateway, pid)
        finally:
            handle.close()
    if not exited:
        _warn_unproven(pid, warn, "the recorded process is not signaled")
    return exited


def _warn_unproven(pid: int, warn: Warn, consequence: str) -> None:
    warn(f"the pane was gone before process {pid} could be checked; {consequence}")


def _exited(gateway: Gateway, pid: int) -> bool:
    """Whether reading ``pid`` shows it gone or a zombie (for a handle that cannot tell)."""
    try:
        process = procs.read(gateway, pid)
    except procs.ProcessError:
        return False
    return process is None or process.zombie


def _kill(
    backend: Backend | None, socket_path: str, pane: ManagedPane, *, query: bool
) -> tuple[bool, tb.ProcessQuery | None]:
    """The guarded ``kill-pane``; whether it ran (False: the pane or the server was already
    gone, which also counts as closed) and, with ``query``, the pane's process read in the
    same invocation."""
    if backend is None:
        return False, None
    outcome, result, answer = backend.kill_pane(pane.pane_id, pane.id, query=query)
    if _gone(outcome, result, socket_path):
        return False, None
    if outcome is tb.GuardOutcome.FAILED:
        raise tb.tmux_failure(result, pane.id, socket_path)
    return True, answer


def _open(gateway: Gateway, pid: int, warn: Warn) -> tuple[ProcessHandle | None, bool]:
    """A handle on ``pid`` and whether it could be opened; (None, True) when no such process
    exists. A handle that cannot be opened warns: nothing is signaled then."""
    try:
        return gateway.open_process(pid), True
    except OSError as err:
        warn(f"cannot open a handle on process {pid}: {err.strerror or err}; not signaling it")
        return None, False


def _uncheckable(verification: Verification, warn: Warn) -> Closed:
    if verification.problem is not None:
        warn(f"{verification.problem}; the recorded process is not signaled")
    return Closed.mismatch()


def _signal_gone_pane(gateway: Gateway, clock: Clock, closing: Closing, warn: Warn) -> Closed:
    """Step 3, pane gone, running at step 1 with ``--force``: the signal sequence only for a
    recorded PID that passed step 1 (its row check and its ``probe``), checking only its
    start time, since there is no ``#{pane_pid}`` to read."""
    first, proof = closing.first, closing.proof
    assert first.pid is not None and first.pid_start is not None
    if proof is None or proof.verification.identity is not Identity.VERIFIED:
        if proof is not None and proof.verification.identity is Identity.GONE:
            return Closed()
        return Closed.mismatch()
    handle, opened = _open(gateway, first.pid, warn)
    if not opened:
        return Closed.mismatch()
    if handle is None:
        return Closed()
    try:
        try:
            process = procs.read(gateway, first.pid)
        except procs.ProcessError as err:
            return _uncheckable(Verification(Identity.UNCHECKABLE, str(err)), warn)
        if handle.exited() or process is None or process.zombie:
            return Closed()
        if process.start != first.pid_start:
            return Closed.mismatch()
        return _sequence(gateway, clock, handle, first, closing, warn)
    finally:
        handle.close()


def _kill_and_signal(
    backend: Backend | None,
    gateway: Gateway,
    clock: Clock,
    socket_path: str,
    closing: Closing,
    warn: Warn,
) -> Closed:
    """Step 5: prove the recorded PID again with a handle already open on it, kill the pane
    reading its ``#{pane_pid}`` in the same invocation, and run the signal sequence only if
    that is still the recorded PID on the same server."""
    current = closing.current
    assert current is not None and current.pid is not None
    handle, opened = _open(gateway, current.pid, warn)
    if not opened:
        _kill(backend, socket_path, current, query=False)
        return Closed.mismatch()
    if handle is None:  # the process has exited: nothing to signal
        _kill(backend, socket_path, current, query=False)
        return Closed()
    try:
        # The handle is open before the process is read, so it names the process verified.
        check = probe(backend, gateway, socket_path, current)
        identity = check.verification.identity
        if check.pane_gone:
            # Gone before the kill: step 3's rule, with the handle proven at step 1.
            return _signal_gone_handle(gateway, clock, closing, handle, warn)
        if identity is not Identity.VERIFIED:
            _kill(backend, socket_path, current, query=False)
            if identity is Identity.GONE:
                return Closed()
            return _uncheckable(check.verification, warn)
        killed, answer = _kill(backend, socket_path, current, query=True)
        if killed and (
            answer is None
            or answer.pane_pid != current.pid
            or answer.server_pid != check.server_pid
        ):
            return Closed.mismatch()  # respawned, or another server: never signaled
        return _sequence(gateway, clock, handle, current, closing, warn)
    finally:
        handle.close()


def _signal_gone_handle(
    gateway: Gateway, clock: Clock, closing: Closing, handle: ProcessHandle, warn: Warn
) -> Closed:
    """The pane vanished between step 3 and the kill: as ``_signal_gone_pane``, with the
    handle already open."""
    proof = closing.proof
    if proof is None or proof.verification.identity is not Identity.VERIFIED:
        return Closed.mismatch()
    return _sequence(gateway, clock, handle, closing.first, closing, warn)


def _sequence(
    gateway: Gateway,
    clock: Clock,
    handle: ProcessHandle,
    pane: ManagedPane,
    closing: Closing,
    warn: Warn,
) -> Closed:
    assert pane.pid is not None and pane.pid_start is not None
    grace_s, sent = closing.grace_s, closing.signals_sent
    outcome = procs.signal_sequence(gateway, clock, handle, pane.pid, pane.pid_start, grace_s, sent)
    for warning in outcome.warnings:
        warn(warning)
    return Closed(outcome.signals_sent, outcome.exited, outcome.pid_mismatch)


# --- --dry-run ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DryRun:
    pid_mismatch: bool
    signals_possible: list[str]


def dry_run(
    backend: Backend | None, gateway: Gateway, socket_path: str, pane: ManagedPane, warn: Warn
) -> DryRun:
    """The check of step 1 for ``--dry-run`` (a running pane here is forced): ``pid_mismatch``
    when the recorded PID fails the ``#{pane_pid}`` check or the process no longer has the
    recorded start time (or cannot be proven, ``probe``); the signals a real run could send
    otherwise. A pane gone at the probe is no mismatch when its recorded process has exited
    too, as ``gone_at_probe`` (reading it only). Read-only: a guarded ``display``,
    ``SO_PEERCRED`` and ``/proc``."""
    mismatch = row_mismatch(pane)
    if pane.dead or pane.pid is None or mismatch:
        return DryRun(mismatch, [])
    found = probe(backend, gateway, socket_path, pane)
    if found.pane_gone:
        if _exited(gateway, pane.pid):
            return DryRun(False, [])
        _warn_unproven(pane.pid, warn, "a real run would not signal the recorded process")
        return DryRun(True, [])
    identity = found.verification
    if identity.identity is Identity.VERIFIED:
        return DryRun(False, list(SIGNALS))
    if identity.identity is Identity.GONE:
        return DryRun(False, [])
    if identity.problem is not None:
        warn(f"{identity.problem}; a real run would not signal the recorded process")
    return DryRun(True, [])
