"""Unit tests for the pane reader (interface.md "Pane options", "Liveness", "Managed panes",
`list`). Real-tmux regressions (spikes F4 and D6) are in test_panes_integration.py."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest

from termctl import ids, panes
from termctl.errors import TermctlError
from termctl.panes import ManagedPane, PaneRead
from termctl.tmux_backend import Tmux

from .fakes import FakeGateway
from .schemas import validate

TMUX = "/usr/bin/tmux"
SOCK = "/tmp/tmux-1000/default"
SENTINEL = "termctl-end-0123456789abcdef"

TMUX_VALUES = {
    "pane_id": "%3",
    "window_id": "@2",
    "pane_dead": "0",
    "pane_dead_status": "",
    "pane_dead_signal": "",
    "pane_pid": "4242",
    "window_panes": "1",
    "session_name": "termctl",
    "window_name": "KEY-12",
}
OPTIONS = {
    "@termctl_pane": "%3",
    "@termctl_v": "1",
    "@termctl_window": "@2",
    "@termctl_id": "tc-abcdefgh",
    "@termctl_name": "KEY-12",
    "@termctl_marker": "",
    "@termctl_marker_text": "",
    "@termctl_labels": "kind=review repo=x",
    "@termctl_placement": "default",
    "@termctl_keep": "never",
    "@termctl_created": "2026-10-30T12:00:00.000Z",
    "@termctl_pid": "",
    "@termctl_pid_start": "",
}


def row(overrides: Mapping[str, str] | None = None, *, sentinel: str = SENTINEL) -> str:
    """One list-panes record as tmux prints it for the reader's format."""
    values = {**TMUX_VALUES, **OPTIONS, **(overrides or {})}
    return "\t".join([*(values[name] for name in panes.FIELDS), sentinel]) + "\n"


def managed(name: str = "%3", *, tc_id: str = "tc-abcdefgh", **extra: str) -> dict[str, str]:
    """Overrides for a managed pane with tmux pane id ``name``."""
    return {"pane_id": name, "@termctl_pane": name, "@termctl_id": tc_id, **extra}


def parse(stdout: str) -> tuple[tuple[ManagedPane, ...], tuple[str, ...]]:
    return panes.parse(stdout, SENTINEL)


def one(overrides: Mapping[str, str]) -> ManagedPane:
    found, warnings = parse(row(overrides))
    assert warnings == ()
    assert len(found) == 1
    return found[0]


def rejected(overrides: Mapping[str, str]) -> str:
    """The single warning for a managed pane that is ignored."""
    found, warnings = parse(row(overrides))
    assert found == ()
    assert len(warnings) == 1, warnings
    return warnings[0]


# --- the format ---------------------------------------------------------------------------------


def test_format_reads_every_field_tab_separated_with_the_sentinel_last() -> None:
    elements = panes.invocation(SENTINEL).commands[0].elements
    assert elements[:3] == ("list-panes", "-a", "-F")
    sanitized = {"session_name", "window_name"}
    expected = [
        f"#{{s/[\t\n]/ /:{name}}}" if name in sanitized else f"#{{{name}}}" for name in panes.FIELDS
    ]
    assert elements[3] == "\t".join([*expected, SENTINEL])
    # Option values are read raw, so a tab or newline in one is detected, never hidden.
    assert set(OPTIONS) <= set(panes.FIELDS)
    assert set(panes.FIELDS) == set(TMUX_VALUES) | set(OPTIONS)


def test_ownership_field_comes_before_any_raw_option() -> None:
    """A tab in a raw option value cannot shift the ownership field."""
    owner = panes.FIELDS.index("@termctl_pane")
    assert all(not name.startswith("@") for name in panes.FIELDS[:owner])


def test_new_sentinel_is_random_and_valid() -> None:
    first, second = panes.new_sentinel(), panes.new_sentinel()
    assert first != second
    panes.invocation(first)  # accepted by the record format


# --- a managed pane -----------------------------------------------------------------------------


def test_parses_a_running_managed_pane() -> None:
    pane = one({})
    assert pane == ManagedPane(
        id="tc-abcdefgh",
        name="KEY-12",
        marker=None,
        marker_text=None,
        labels=("kind=review", "repo=x"),
        placement="default",
        keep_on_exit="never",
        created="2026-10-30T12:00:00.000Z",
        opened_window="@2",
        pid=None,
        pid_start=None,
        pane_id="%3",
        window_id="@2",
        session="termctl",
        sessions=("termctl",),
        display_name="KEY-12",
        pane_pid=4242,
        window_panes=1,
        dead=False,
        exit_status=None,
        exit_signal=None,
    )
    assert pane.state == "running"


def test_empty_output_has_no_panes() -> None:
    assert parse("") == ((), ())


def test_marker_and_pid_are_read() -> None:
    pane = one(
        {
            "@termctl_marker": "attention",
            "@termctl_marker_text": "\U0001f514 ",
            "@termctl_pid": "4242",
            "@termctl_pid_start": "123456",
        }
    )
    assert (pane.marker, pane.marker_text) == ("attention", "\U0001f514 ")
    assert (pane.pid, pane.pid_start) == (4242, "123456")


def test_empty_labels_are_none() -> None:
    assert one({"@termctl_labels": ""}).labels == ()


def test_unicode_name_and_display_name_are_kept() -> None:
    pane = one({"@termctl_name": "PR#42 review é中", "window_name": "\U0001f514 PR#42 \\033"})
    assert pane.name == "PR#42 review é中"
    assert pane.display_name == "\U0001f514 PR#42 \\033"


@pytest.mark.parametrize(("status", "signal"), [("3", ""), ("0", ""), ("", "9"), ("", "")])
def test_dead_pane_reports_its_exit(status: str, signal: str) -> None:
    pane = one({"pane_dead": "1", "pane_dead_status": status, "pane_dead_signal": signal})
    assert pane.state == "dead"
    assert pane.exit_status == (int(status) if status else None)
    assert pane.exit_signal == (int(signal) if signal else None)


def test_running_pane_has_no_exit_fields() -> None:
    pane = one({"pane_dead": "0", "pane_dead_status": "", "pane_dead_signal": ""})
    assert (pane.exit_status, pane.exit_signal) == (None, None)


def test_dead_pane_without_pane_pid() -> None:
    assert one({"pane_dead": "1", "pane_pid": ""}).pane_pid is None


def test_display_name_may_be_empty() -> None:
    assert one({"window_name": ""}).display_name == ""


def test_32_labels_are_accepted() -> None:
    labels = " ".join(sorted(f"k{i:02d}=v" for i in range(32)))
    assert len(one({"@termctl_labels": labels}).labels) == 32


# --- ownership (spike F4) -----------------------------------------------------------------------


@pytest.mark.parametrize("owner", ["", "%4", "%30", "%3 ", " %3", "x", "%3\n"])
def test_a_pane_whose_ownership_value_is_not_its_own_id_is_not_managed(owner: str) -> None:
    """Values inherited from the window, session or global scope name another pane (or none);
    such panes are not managed and are skipped without a warning."""
    assert parse(row({"@termctl_pane": owner})) == ((), ())


def test_a_tab_after_the_own_id_is_flagged_never_managed() -> None:
    found, warnings = parse(row({"@termctl_pane": "%3\t"}))
    assert found == () and len(warnings) == 1


def test_unmanaged_panes_with_garbage_options_raise_no_warning() -> None:
    stdout = row({"@termctl_pane": "", "@termctl_name": "a\tb\nc", "@termctl_v": "9"})
    assert parse(stdout) == ((), ())


def test_only_the_owning_pane_is_managed_when_a_value_is_inherited() -> None:
    """A window-level @termctl_pane naming %3 shows in every pane of that window; only %3
    passes."""
    stdout = row(managed("%3")) + row({"pane_id": "%4", "@termctl_pane": "%3"})
    found, warnings = parse(stdout)
    assert [p.pane_id for p in found] == ["%3"] and warnings == ()


# --- option grammars ----------------------------------------------------------------------------

BAD_OPTIONS = [
    ("@termctl_v", ""),
    ("@termctl_v", "2"),
    ("@termctl_v", "1 "),
    ("@termctl_v", "01"),
    ("@termctl_window", ""),
    ("@termctl_window", "2"),
    ("@termctl_window", "@x"),
    ("@termctl_window", "@2\n"),
    ("@termctl_id", ""),
    ("@termctl_id", "tc-abcdefg"),
    ("@termctl_id", "tc-abcdefgh1"),
    ("@termctl_id", "TC-ABCDEFGH"),
    ("@termctl_id", "tc-abcdefg1"),
    ("@termctl_id", "abcdefgh"),
    ("@termctl_name", ""),
    ("@termctl_name", "x" * 65),
    ("@termctl_name", "a\nb"),
    ("@termctl_name", "a\x85b"),
    ("@termctl_name", "a\x7fb"),
    ("@termctl_name", "a\udcffb"),
    ("@termctl_name", "a#[fg=red]"),
    ("@termctl_marker", "Attention"),
    ("@termctl_marker", "-x"),
    ("@termctl_marker", "a" * 33),
    ("@termctl_labels", "kind=review  repo=x"),
    ("@termctl_labels", " kind=review"),
    ("@termctl_labels", "kind=review "),
    ("@termctl_labels", "repo=x kind=review"),
    ("@termctl_labels", "kind=review kind=review"),
    ("@termctl_labels", "kind"),
    ("@termctl_labels", "Kind=x"),
    ("@termctl_labels", "kind=a b=c d"),
    ("@termctl_labels", "kind=x;y"),
    ("@termctl_labels", "kind=x\ny=1"),
    ("@termctl_labels", " ".join(sorted(f"k{i:02d}=v" for i in range(33)))),
    ("@termctl_placement", ""),
    ("@termctl_placement", "Default"),
    ("@termctl_placement", "a b"),
    ("@termctl_keep", ""),
    ("@termctl_keep", "on"),
    ("@termctl_keep", "Never"),
    ("@termctl_created", ""),
    ("@termctl_created", "2026-10-30T12:00:00Z"),
    ("@termctl_created", "2026-10-30T12:00:00.000+00:00"),
    ("@termctl_created", "2026-10-30 12:00:00.000Z"),
    ("@termctl_created", "2026-02-30T12:00:00.000Z"),
    ("@termctl_created", "2026-10-30T24:00:00.000Z"),
    ("@termctl_created", "2026-10-30T12:00:00.0000Z"),
    ("@termctl_created", "２026-10-30T12:00:00.000Z"),
    ("@termctl_pid", "0"),
    ("@termctl_pid", "012"),
    ("@termctl_pid", "12345678901"),
    ("@termctl_pid", "-1"),
    ("@termctl_pid", "١٢"),
    ("@termctl_pid_start", "x"),
    ("@termctl_pid_start", "1" * 21),
]


@pytest.mark.parametrize(("option", "value"), BAD_OPTIONS)
def test_every_option_grammar_is_enforced(option: str, value: str) -> None:
    warning = rejected({option: value})
    assert "%3" in warning and option in warning


def test_an_unknown_schema_version_is_ignored_with_a_warning() -> None:
    warning = rejected({"@termctl_v": "2", "@termctl_name": ""})
    assert "@termctl_v" in warning and "'2'" in warning


@pytest.mark.parametrize(
    ("marker", "text"), [("attention", ""), ("", "\U0001f514 "), ("attention", "x#")]
)
def test_marker_and_marker_text_go_together(marker: str, text: str) -> None:
    assert "@termctl_marker" in rejected({"@termctl_marker": marker, "@termctl_marker_text": text})


@pytest.mark.parametrize(("pid", "start"), [("4242", ""), ("", "123")])
def test_pid_and_pid_start_go_together(pid: str, start: str) -> None:
    assert "@termctl_pid" in rejected({"@termctl_pid": pid, "@termctl_pid_start": start})


def test_a_tab_in_an_option_is_a_malformed_record_not_a_hidden_space() -> None:
    warning = rejected({"@termctl_name": "a\tb"})
    assert "%3" in warning


def test_warnings_escape_and_bound_untrusted_values() -> None:
    warning = rejected({"@termctl_name": "\x9b2J" + "x" * 500})
    assert "\x9b" not in warning
    assert len(warning) < 300


def test_one_bad_pane_does_not_hide_the_others() -> None:
    stdout = (
        row(managed("%1", tc_id="tc-aaaaaaaa"))
        + row(managed("%2", tc_id="tc-bbbbbbbb", **{"@termctl_keep": "bogus"}))
        + row(managed("%3", tc_id="tc-cccccccc"))
    )
    found, warnings = parse(stdout)
    assert [p.id for p in found] == ["tc-aaaaaaaa", "tc-cccccccc"]
    assert len(warnings) == 1 and "%2" in warnings[0]


# --- tmux's fields ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_id", ""),
        ("window_id", "2"),
        ("pane_dead", ""),
        ("pane_dead", "2"),
        ("pane_dead_status", "x"),
        ("pane_dead_status", "-1"),
        ("pane_dead_signal", "x"),
        ("pane_pid", "abc"),
        ("pane_pid", "-5"),
        ("window_panes", ""),
        ("window_panes", "0"),
        ("session_name", ""),
    ],
)
def test_malformed_tmux_fields_are_ignored_with_a_warning(field: str, value: str) -> None:
    assert "%3" in rejected({field: value})


def test_both_exit_fields_set_is_malformed() -> None:
    rejected({"pane_dead": "1", "pane_dead_status": "1", "pane_dead_signal": "9"})


# --- records and the sentinel -------------------------------------------------------------------


def test_a_record_without_the_sentinel_is_ignored_with_a_warning() -> None:
    stdout = row(managed("%1", tc_id="tc-aaaaaaaa")) + row(managed("%2"), sentinel="wrong")
    found, warnings = parse(stdout)
    assert [p.pane_id for p in found] == ["%1"]
    assert len(warnings) == 1


def test_truncated_output_is_ignored_with_a_warning() -> None:
    stdout = row(managed("%1", tc_id="tc-aaaaaaaa")) + row(managed("%2"))[:40]
    found, warnings = parse(stdout)
    assert [p.pane_id for p in found] == ["%1"]
    assert len(warnings) == 1


def test_an_option_cannot_inject_a_record() -> None:
    """A forged value holding a newline and a whole record (with a guessed sentinel) produces
    no pane: records end only with this read's random sentinel."""
    fake = row(managed("%9", tc_id="tc-zzzzzzzz"), sentinel="termctl-end-ffffffffffffffff")
    found, warnings = parse(row({"@termctl_name": "x\n" + fake.rstrip("\n")}))
    assert found == ()
    assert len(warnings) == 1 and "%3" in warnings[0]


def test_an_option_holding_the_terminator_shape_cannot_split_the_record() -> None:
    forged = "x\t" + "termctl-end-ffffffffffffffff"
    found, warnings = parse(row({"@termctl_labels": forged}))
    assert found == () and len(warnings) == 1


def test_an_unreadable_record_is_reported() -> None:
    found, warnings = parse("garbage\t" + SENTINEL + "\n")
    assert found == ()
    assert len(warnings) == 1


# --- linked windows (spike D6) ------------------------------------------------------------------


def test_rows_of_a_linked_window_are_merged() -> None:
    stdout = (
        row({"session_name": "first"})
        + row({"session_name": "second"})
        + row({"session_name": "first"})
    )
    found, warnings = parse(stdout)
    assert warnings == ()
    assert len(found) == 1
    assert found[0].session == "first"
    assert found[0].sessions == ("first", "second")


def test_a_bad_pane_in_a_linked_window_warns_once() -> None:
    bad = {"@termctl_keep": "bogus"}
    stdout = row({**bad, "session_name": "a"}) + row({**bad, "session_name": "b"})
    found, warnings = parse(stdout)
    assert found == () and len(warnings) == 1


def test_two_panes_carrying_one_id_are_both_kept() -> None:
    """Resolution reports them as ambiguous (ids.resolve); the reader keeps both."""
    found, _ = parse(row(managed("%3")) + row(managed("%4")))
    assert [p.pane_id for p in found] == ["%3", "%4"]
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("tc-abcdefgh"), found)
    assert info.value.code == "ambiguous"


def test_managed_panes_resolve_by_id() -> None:
    found, _ = parse(row(managed("%3")) + row(managed("%4", tc_id="tc-zzzzzzzz")))
    assert ids.resolve(ids.parse("abcd"), found).pane_id == "%3"


# --- the pane object ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"pane_dead": "1", "pane_dead_status": "3"},
        {"pane_dead": "1", "pane_dead_signal": "9"},
        {"pane_dead": "1"},
        {"@termctl_marker": "attention", "@termctl_marker_text": "\U0001f514 "},
        {"@termctl_pid": "4242", "@termctl_pid_start": "99", "@termctl_labels": ""},
        {"window_name": "", "session_name": "my work", "@termctl_name": "-x"},
    ],
)
def test_pane_object_matches_the_schema(overrides: dict[str, str]) -> None:
    obj = one(overrides).pane_object(SOCK)
    validate(obj, "termctl/cli/pane.json")
    assert obj["socket_path"] == SOCK


def test_pane_object_fields() -> None:
    obj = one({"@termctl_pid": "4242", "@termctl_pid_start": "99"}).pane_object(SOCK)
    assert obj == {
        "id": "tc-abcdefgh",
        "name": "KEY-12",
        "display_name": "KEY-12",
        "marker": None,
        "marker_text": None,
        "labels": ["kind=review", "repo=x"],
        "placement": "default",
        "session": "termctl",
        "pane_id": "%3",
        "window_id": "@2",
        "socket_path": SOCK,
        "created": "2026-10-30T12:00:00.000Z",
        "pid": 4242,
        "state": "running",
        "exit_status": None,
        "exit_signal": None,
        "keep_on_exit": "never",
    }


# --- filters and order --------------------------------------------------------------------------


def _pane(pane_id: str, tc_id: str, created: str, **extra: str) -> str:
    return row(managed(pane_id, tc_id=tc_id, **{"@termctl_created": created, **extra}))


def _listed() -> tuple[ManagedPane, ...]:
    stdout = (
        _pane("%1", "tc-cccccccc", "2026-10-30T12:00:01.000Z", **{"@termctl_labels": "a=1"})
        + _pane("%2", "tc-bbbbbbbb", "2026-10-30T12:00:00.000Z", **{"@termctl_labels": "a=1 b=2"})
        + _pane("%3", "tc-aaaaaaaa", "2026-10-30T12:00:00.000Z", pane_dead="1", session_name="x y")
    )
    found, warnings = parse(stdout)
    assert warnings == ()
    return found


def test_select_orders_by_created_then_id() -> None:
    assert [p.id for p in panes.select(_listed())] == [
        "tc-aaaaaaaa",
        "tc-bbbbbbbb",
        "tc-cccccccc",
    ]


def test_select_ands_label_filters() -> None:
    assert [p.id for p in panes.select(_listed(), labels=["a=1"])] == [
        "tc-bbbbbbbb",
        "tc-cccccccc",
    ]
    assert [p.id for p in panes.select(_listed(), labels=["a=1", "b=2"])] == ["tc-bbbbbbbb"]
    assert panes.select(_listed(), labels=["a=2"]) == []
    assert panes.select(_listed(), labels=["a="]) == []


def test_select_by_session_matches_exactly() -> None:
    assert [p.id for p in panes.select(_listed(), session="x y")] == ["tc-aaaaaaaa"]
    assert panes.select(_listed(), session="x") == []
    assert panes.select(_listed(), session="termct") == []


def test_select_by_session_matches_any_linked_session() -> None:
    found, _ = parse(row({"session_name": "first"}) + row({"session_name": "second"}))
    assert len(panes.select(found, session="second")) == 1
    assert panes.select(found, session="third") == []


def test_select_dead_only() -> None:
    assert [p.id for p in panes.select(_listed(), dead=True)] == ["tc-aaaaaaaa"]


# --- reading through tmux -----------------------------------------------------------------------


def _tmux(rc: int = 0, out: str = "", err: str = "") -> tuple[Tmux, FakeGateway]:
    argv = panes.invocation(SENTINEL).argv(TMUX, SOCK, utf8=True)
    gateway = FakeGateway({tuple(argv): subprocess.CompletedProcess(argv, rc, out, err)})
    return Tmux(gateway, TMUX, SOCK, env={"PATH": "/usr/bin"}), gateway


def test_read_runs_one_list_panes_with_utf8_output() -> None:
    tmux, gateway = _tmux(out=row({}))
    result = panes.read(tmux, sentinel=SENTINEL)
    assert result == PaneRead(server_running=True, panes=(one({}),), warnings=())
    assert len(gateway.calls) == 1
    assert gateway.calls[0][:6] == [TMUX, "-u", "-S", SOCK, "list-panes", "-a"]


@pytest.mark.parametrize(
    "stderr",
    [
        f"no server running on {SOCK}\n",
        f"error connecting to {SOCK} (No such file or directory)\n",
    ],
)
def test_read_reports_no_server(stderr: str) -> None:
    tmux, _ = _tmux(rc=1, err=stderr)
    assert panes.read(tmux, sentinel=SENTINEL) == PaneRead(
        server_running=False, panes=(), warnings=()
    )


@pytest.mark.parametrize(
    "stderr",
    [
        f"error connecting to {SOCK} (Permission denied)\n",
        "server exited unexpectedly\n",
        "",
        "no current target: %1\n",
        "something else\nno current target\n",
        "no current target\nsomething else\n",
    ],
)
def test_read_maps_other_failures_to_server_unavailable(stderr: str) -> None:
    tmux, _ = _tmux(rc=1, err=stderr)
    with pytest.raises(TermctlError) as info:
        panes.read(tmux, sentinel=SENTINEL)
    assert info.value.code == "server_unavailable"


def test_read_of_a_server_without_sessions_is_running_with_no_panes() -> None:
    """tmux 3.5a: list-panes -a on a server with no sessions exits 1, "no current target"."""
    tmux, _ = _tmux(rc=1, err="no current target\n")
    assert panes.read(tmux, sentinel=SENTINEL) == PaneRead(
        server_running=True, panes=(), warnings=()
    )


def test_read_generates_a_fresh_sentinel_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Capture(FakeGateway):
        def run(self, argv, *, stdin=None, timeout=None, env=None, new_session=False):  # type: ignore[no-untyped-def,override]
            seen.append(argv[-1].rsplit("\t", 1)[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

    tmux = Tmux(Capture(), TMUX, SOCK, env={})
    panes.read(tmux)
    panes.read(tmux)
    assert len(set(seen)) == 2
