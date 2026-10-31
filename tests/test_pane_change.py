"""The locked, guarded read-modify-write of a managed pane (interface.md "Names", `rename`,
"Audit and log" Locking, "tmux argument rules" rule 5, "Residual risks" forged control bytes)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from termctl import lock, pane_change, panes
from termctl.errors import TermctlError
from termctl.pane_change import Change
from termctl.panes import ManagedPane, PaneRead
from termctl.tmux_bin import TmuxVersion

from .fakes import FakeClock, list_panes_stdout, pane_row

SENTINEL = "termctl-end-0123456789abcdef"
SOCK = "/tmp/tmux-1000/default"
ID = "tc-abcdefgh"
V35 = TmuxVersion(3, 5, "3.5a")
V32 = TmuxVersion(3, 2, "3.2a")


def managed(**overrides: str) -> ManagedPane:
    found, warnings = panes.parse(
        list_panes_stdout([pane_row(overrides=overrides)], SENTINEL), SENTINEL
    )
    assert warnings == ()
    return found[0]


@dataclass
class FakeBackend:
    """An in-memory server: ``reads`` are the successive ``read_panes`` answers (the last one
    repeats); ``write_pane`` applies a matching write to the next read's pane."""

    reads: list[PaneRead]
    lengths: tuple[int, ...] | None = None
    window_renamed: bool = True
    writes: list[tuple[str, dict[str, str], str | None]] = field(default_factory=list)
    length_queries: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    lock_held_during: list[str] = field(default_factory=list)
    socket_path: str = SOCK

    def read_panes(self) -> PaneRead:
        self.lock_held_during.append(f"read {lock.held()}")
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def write_pane(
        self, pane: ManagedPane, options: Mapping[str, str], window_name: str | None
    ) -> bool:
        self.lock_held_during.append(f"write {lock.held()}")
        self.writes.append((pane.pane_id, dict(options), window_name))
        return window_name is not None and self.window_renamed

    def byte_lengths(self, pane: ManagedPane, names: Sequence[str]) -> tuple[int, ...] | None:
        self.length_queries.append((pane.pane_id, tuple(names)))
        return self.lengths


def read(*found: ManagedPane, warnings: tuple[str, ...] = ()) -> PaneRead:
    return PaneRead(server_running=True, panes=found, warnings=warnings)


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state")}


def change(
    backend: FakeBackend,
    plan: Change,
    environ: Mapping[str, str],
    version: TmuxVersion | None = V35,
    warned: list[str] | None = None,
    seen: Sequence[str] = (),
) -> pane_change.Outcome:
    sink = [] if warned is None else warned
    return pane_change.change_pane(
        backend, ID, lambda pane: plan, version=version, environ=environ, clock=FakeClock(),
        warn=sink.append, seen=seen,
    )  # fmt: skip


def renamed(name: str) -> Change:
    return Change(options={"@termctl_name": name}, display_name=name)


# --- writes ----------------------------------------------------------------------------------


def test_a_change_writes_under_the_lock_and_reads_the_pane_again(environ: dict[str, str]) -> None:
    before = managed()
    after = replace(before, name="new", display_name="new")
    backend = FakeBackend([read(before), read(after)])
    outcome = change(backend, renamed("new"), environ)
    assert outcome == pane_change.Outcome(pane=after, changed=True, display_updated=True)
    assert backend.writes == [("%3", {"@termctl_name": "new"}, "new")]
    assert backend.lock_held_during == ["read True", "write True", "read True"]
    assert not lock.held()


def test_the_plan_sees_the_pane_as_read_under_the_lock(environ: dict[str, str]) -> None:
    fresh = managed(**{"@termctl_name": "fresh"})
    seen: list[ManagedPane] = []

    def plan(pane: ManagedPane) -> Change:
        seen.append(pane)
        return renamed("x")

    backend = FakeBackend([read(fresh)])
    pane_change.change_pane(
        backend, ID, plan, version=V35, environ=environ, clock=FakeClock(), warn=print
    )
    assert seen == [fresh]


def test_an_ineligible_window_gets_the_options_only(environ: dict[str, str]) -> None:
    split = managed(window_panes="2", window_name="user's")
    backend = FakeBackend([read(split)])
    outcome = change(backend, renamed("new"), environ)
    assert (outcome.changed, outcome.display_updated) == (True, False)
    assert backend.writes == [("%3", {"@termctl_name": "new"}, None)]


def test_a_moved_pane_gets_the_options_only(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed(window_id="@9", window_name="other"))])
    change(backend, renamed("new"), environ)
    assert backend.writes == [("%3", {"@termctl_name": "new"}, None)]


def test_a_window_split_after_the_read_reports_display_not_updated(
    environ: dict[str, str],
) -> None:
    backend = FakeBackend([read(managed())], window_renamed=False)
    outcome = change(backend, renamed("new"), environ)
    assert (outcome.changed, outcome.display_updated) == (True, False)


def test_a_change_without_a_display_name_never_renames(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed())])
    outcome = change(backend, Change({"@termctl_labels": "a=b"}, None), environ)
    assert (outcome.changed, outcome.display_updated) == (True, False)
    assert backend.writes == [("%3", {"@termctl_labels": "a=b"}, None)]


def test_same_options_but_a_stale_window_name_renames_it(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed(window_name="renamed by someone"))])
    outcome = change(backend, renamed("KEY-12"), environ)
    assert (outcome.changed, outcome.display_updated) == (True, True)
    assert backend.writes == [("%3", {"@termctl_name": "KEY-12"}, "KEY-12")]


def test_options_a_change_cannot_write_are_refused(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed())])
    with pytest.raises(ValueError):
        change(backend, Change({"@termctl_pane": "%1"}, None), environ)
    assert backend.writes == []


# --- the pane under the lock -------------------------------------------------------------------


def test_a_pane_gone_before_the_lock_is_not_found(environ: dict[str, str]) -> None:
    backend = FakeBackend([read()])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ)
    assert caught.value.exit_code == 10
    assert backend.writes == []


def test_warnings_of_the_read_under_the_lock_are_reported(environ: dict[str, str]) -> None:
    # The pane turned invalid while termctl waited for the lock: it is gone, with the reason.
    backend = FakeBackend([read(warnings=("ignoring managed pane %3: bad",))])
    warned: list[str] = []
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ, warned=warned)
    assert caught.value.exit_code == 10
    assert warned == ["ignoring managed pane %3: bad"]


def test_warnings_are_reported_once(environ: dict[str, str]) -> None:
    pane = managed()
    after = replace(pane, name="new", display_name="new")
    backend = FakeBackend(
        [read(pane, warnings=("old", "w1")), read(after, warnings=("w1", "w2", "old"))]
    )
    warned: list[str] = []
    change(backend, renamed("new"), environ, warned=warned, seen=("old",))
    assert warned == ["w1", "w2"]


def test_a_server_gone_before_the_lock_is_not_found(environ: dict[str, str]) -> None:
    backend = FakeBackend([PaneRead(server_running=False, panes=(), warnings=())])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ)
    assert (caught.value.code, caught.value.exit_code) == ("not_found", 10)


def test_a_copy_appearing_before_the_lock_is_ambiguous(environ: dict[str, str]) -> None:
    copy = replace(managed(), pane_id="%9")
    backend = FakeBackend([read(managed(), copy)])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ)
    assert caught.value.exit_code == 21
    assert backend.writes == []


def test_a_pane_gone_after_the_write_is_not_found(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed()), read()])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ)
    assert caught.value.exit_code == 10
    assert not lock.held()


def test_the_id_is_matched_in_full_under_the_lock(environ: dict[str, str]) -> None:
    # A pane whose id merely starts like ours is another pane.
    other = managed(**{"@termctl_id": "tc-abcdefgz"})
    backend = FakeBackend([read(other)])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("new"), environ)
    assert caught.value.exit_code == 10


# --- no-ops ------------------------------------------------------------------------------------


def test_nothing_to_change_is_a_no_op_without_a_write(environ: dict[str, str]) -> None:
    pane = managed()
    backend = FakeBackend([read(pane)])
    outcome = change(backend, renamed("KEY-12"), environ)
    assert outcome == pane_change.Outcome(pane=pane, changed=False, display_updated=False)
    assert backend.writes == []
    assert backend.length_queries == []


def test_an_ineligible_window_name_is_not_compared(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed(window_panes="2", window_name="user's"))])
    assert change(backend, renamed("KEY-12"), environ).changed is False
    assert backend.writes == []


def test_a_doubled_backslash_window_name_is_proven_by_byte_lengths(
    environ: dict[str, str],
) -> None:
    pane = managed(**{"@termctl_name": "a\\b", "window_name": "a\\\\b"})
    backend = FakeBackend([read(pane)], lengths=(3, 4))
    assert change(backend, renamed("a\\b"), environ).changed is False
    assert backend.length_queries == [("%3", ("@termctl_name", "window_name"))]


def test_a_forged_control_byte_in_the_option_is_rewritten(environ: dict[str, str]) -> None:
    # "n" ESC "x" reads back as the text n\033x (4 bytes more than stored: 3 against 6).
    pane = managed(**{"@termctl_name": "n\\033x", "window_name": "n\\\\033x"})
    backend = FakeBackend([read(pane)], lengths=(3, 7))
    outcome = change(backend, renamed("n\\033x"), environ)
    assert outcome.changed is True
    assert backend.writes == [("%3", {"@termctl_name": "n\\033x"}, "n\\033x")]


def test_a_forged_control_byte_in_the_window_name_is_rewritten(environ: dict[str, str]) -> None:
    pane = managed(**{"@termctl_name": "n\\033x", "window_name": "n\\033x"})
    backend = FakeBackend([read(pane)], lengths=(6, 3))
    assert change(backend, renamed("n\\033x"), environ).changed is True


def test_unreadable_byte_lengths_are_not_a_no_op(environ: dict[str, str]) -> None:
    pane = managed(**{"@termctl_name": "a\\b", "window_name": "a\\b"})
    backend = FakeBackend([read(pane)], lengths=None)
    assert change(backend, renamed("a\\b"), environ).changed is True


def test_only_values_holding_a_backslash_are_measured(environ: dict[str, str]) -> None:
    pane = managed(**{"@termctl_name": "KEY", "@termctl_marker": "attention",
                      "@termctl_marker_text": "\\o/ ", "window_name": "\\\\o/ KEY"})  # fmt: skip
    backend = FakeBackend([read(pane)], lengths=(8,))
    plan = Change({"@termctl_name": "KEY"}, "\\o/ KEY")
    assert change(backend, plan, environ).changed is False
    assert backend.length_queries == [("%3", ("window_name",))]


def test_multibyte_text_is_measured_in_bytes(environ: dict[str, str]) -> None:
    name = "\\é中\U0001f514"
    pane = managed(**{"@termctl_name": name, "window_name": name})
    backend = FakeBackend([read(pane)], lengths=(10, 10))
    assert change(backend, renamed(name), environ).changed is False


def test_a_window_name_holding_a_space_is_measured(environ: dict[str, str]) -> None:
    # The reader shows a tab or newline in a window name as a space: the probe must prove
    # there is none (TmuxBackend.byte_lengths) before "a b" counts as unchanged.
    pane = managed(**{"@termctl_name": "a b", "window_name": "a b"})
    backend = FakeBackend([read(pane)], lengths=(3,))
    assert change(backend, renamed("a b"), environ).changed is False
    assert backend.length_queries == [("%3", ("window_name",))]


def test_a_window_name_with_a_space_that_cannot_be_proven_is_rewritten(
    environ: dict[str, str],
) -> None:
    pane = managed(**{"@termctl_name": "a b", "window_name": "a b"})
    backend = FakeBackend([read(pane)], lengths=None)
    assert change(backend, renamed("a b"), environ).changed is True
    assert backend.writes == [("%3", {"@termctl_name": "a b"}, "a b")]


def test_an_option_holding_a_space_is_not_measured(environ: dict[str, str]) -> None:
    pane = managed(**{"@termctl_name": "a b", "window_name": "user's", "window_panes": "2"})
    backend = FakeBackend([read(pane)])
    assert change(backend, renamed("a b"), environ).changed is False
    assert backend.length_queries == []


def test_tmux_3_2_dollar_escaping_is_a_no_op(environ: dict[str, str]) -> None:
    pane = managed(**{"@termctl_name": "a$b", "window_name": "a\\$b"})
    backend = FakeBackend([read(pane)], lengths=(4,))
    assert change(backend, renamed("a$b"), environ, version=V32).changed is False
    assert change(backend, renamed("a$b"), environ, version=V35).changed is True


def test_a_no_op_still_takes_the_lock(environ: dict[str, str]) -> None:
    backend = FakeBackend([read(managed())])
    change(backend, renamed("KEY-12"), environ)
    assert backend.lock_held_during == ["read True"]


# --- the lock ----------------------------------------------------------------------------------

HOLDER = """
import sys, time
from termctl import lock
from termctl.gateway import SystemClock
with lock.server_lock(sys.argv[2], environ={"XDG_STATE_HOME": sys.argv[1]}, clock=SystemClock()):
    print("held", flush=True)
    sys.stdin.read()
"""


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path / "state"), SOCK],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert proc.stdout is not None and proc.stdout.readline() == "held\n"
    try:
        yield proc
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)


class SteppingClock(FakeClock):
    """Moves one second on every ``monotonic`` call, so a lock wait times out quickly."""

    def monotonic(self) -> float:
        self.advance(1.0)
        return super().monotonic()


def test_a_held_lock_times_out_as_locked_without_touching_tmux(
    environ: dict[str, str], holder: subprocess.Popen[str]
) -> None:
    backend = FakeBackend([read(managed())])
    with pytest.raises(TermctlError) as caught:
        pane_change.change_pane(
            backend, ID, lambda pane: renamed("x"), version=V35, environ=environ,
            clock=SteppingClock(), warn=print,
        )  # fmt: skip
    assert (caught.value.code, caught.value.exit_code) == ("locked", 12)
    assert caught.value.details is not None and caught.value.details["cause"] == "timeout"
    assert backend.lock_held_during == []


def test_an_unusable_state_directory_is_locked_unusable(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("not a directory")
    backend = FakeBackend([read(managed())])
    with pytest.raises(TermctlError) as caught:
        change(backend, renamed("x"), {"XDG_STATE_HOME": str(tmp_path / "state")})
    assert caught.value.code == "locked"
    assert caught.value.details is not None and caught.value.details["cause"] == "unusable"
    assert backend.lock_held_during == []
