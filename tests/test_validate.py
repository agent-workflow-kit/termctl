"""Positive and negative tables for every input grammar in interface.md."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from termctl import validate
from termctl.errors import TermctlError, UsageError

from .fakes import FakeGateway
from .schemas import SCHEMAS

DEFS = SCHEMAS["termctl/defs.json"]["$defs"]

# Control characters: C0 (tab and newline included), DEL and C1.
CONTROL = ["\x00", "\x01", "\t", "\n", "\r", "\x1b", "\x1f", "\x7f", "\x80", "\x85", "\x9b", "\x9f"]
# Values that look like injections into tmux or a shell; harmless for the ASCII grammars.
INJECTIONS = ["a;b", "a b", "$(id)", "`id`", "a|b", "'a'", '"a"', "a\\b", "#(id)", "#[fg=red]"]


def search(pattern: str, value: str) -> bool:
    """JSON Schema (ECMA-262) pattern search: ``$`` never matches before a final newline,
    unlike Python's, so it becomes ``\\Z``."""
    return re.search(pattern.replace("$", r"\Z"), value) is not None


def schema_matches(definition: str, value: str) -> bool:
    """A cross-check of the grammar against ``schemas/defs.json`` (pattern and lengths)."""
    schema = DEFS[definition]
    if "minLength" in schema and len(value) < schema["minLength"]:
        return False
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        return False
    return search(schema["pattern"], value)


def raises_usage(func: Callable[..., Any], *args: Any) -> UsageError:
    with pytest.raises(UsageError) as info:
        func(*args)
    assert info.value.exit_code == 2
    return info.value


# --- names --------------------------------------------------------------------------------------

NAMES_OK = [
    "a",
    "KEY-12",
    "PR#42 review",
    "x" * 64,
    "日本語のタスク",
    "🔔 bell",
    "é" * 64,  # 64 code points, 128 bytes
    "trailing#",
    "#",
    "##",
    "a;",
    ";",
    "-leading-dash",
    "--json",
    "$(rm -rf ~)",
    "#(id)",
    "#{pane_id}",
    "# [spaced]",
    "[#",
    "a\u202eb",  # right-to-left override: not a control character, accepted (names are trusted)
    "\u2028",  # line separator: not C0, DEL or C1
    "\u00a0",  # no-break space
]
NAMES_BAD = [
    "",
    "x" * 65,
    "x" * 10_000,
    "#[",
    "a#[fg=red]",
    "##[",
    *[f"a{c}b" for c in CONTROL],
    "\ud800",  # a lone surrogate: undecodable bytes from argv, not text
    "a\udcffb",
]


@pytest.mark.parametrize("value", NAMES_OK)
def test_name_ok(value: str) -> None:
    assert validate.is_name(value)
    assert validate.check_name(value) == value


@pytest.mark.parametrize("value", NAMES_BAD)
def test_name_bad(value: str) -> None:
    assert not validate.is_name(value)
    raises_usage(validate.check_name, value)


@pytest.mark.parametrize(
    "value", [v for v in NAMES_OK + NAMES_BAD if not any("\ud800" <= c <= "\udfff" for c in v)]
)
def test_name_agrees_with_schema(value: str) -> None:
    assert validate.is_name(value) == schema_matches("name", value)


def test_name_error_does_not_echo_control_characters_or_overlong_input() -> None:
    err = raises_usage(validate.check_name, "\x1b]0;evil\x07" + "x" * 5000)
    assert "\x1b" not in err.message and "\x07" not in err.message
    assert len(err.message) < 300


# --- markers ------------------------------------------------------------------------------------

MARKER_NAMES_OK = ["a", "0", "attention", "needs_input", "w-1", "a" * 32, "9lives"]
MARKER_NAMES_BAD = [
    "",
    "a" * 33,
    "Attention",
    "-a",
    "_a",
    "a.b",
    "a b",
    "a\n",
    "é",
    "a;",
    "#(id)",
    "\x00",
    "ａ",  # fullwidth
]


@pytest.mark.parametrize("value", MARKER_NAMES_OK)
def test_marker_name_ok(value: str) -> None:
    assert validate.is_marker_name(value)
    assert validate.check_marker_name(value) == value
    assert schema_matches("marker_name", value)


@pytest.mark.parametrize("value", MARKER_NAMES_BAD)
def test_marker_name_bad(value: str) -> None:
    assert not validate.is_marker_name(value)
    raises_usage(validate.check_marker_name, value)
    assert not schema_matches("marker_name", value)


MARKER_TEXTS_OK = ["🔔 ", "!", "x" * 16, "[wait] ", "#a", "a#b", "# ", "(#)", "é" * 16]
MARKER_TEXTS_BAD = [
    "",
    "x" * 17,
    "#",
    "a#",
    "##",
    "#[",
    "a#[b",
    "\t",
    "a\x9b",
    "a\x7f",
    "\ud83d",
]


@pytest.mark.parametrize("value", MARKER_TEXTS_OK)
def test_marker_text_ok(value: str) -> None:
    assert validate.is_marker_text(value)
    assert validate.check_marker_text(value) == value
    assert schema_matches("marker_text", value)


@pytest.mark.parametrize("value", MARKER_TEXTS_BAD)
def test_marker_text_bad(value: str) -> None:
    assert not validate.is_marker_text(value)
    raises_usage(validate.check_marker_text, value)


def test_marker_text_ending_in_hash_would_form_style_with_a_name() -> None:
    # "!#" + "[x]" would display as "!#[x]": rejected even though neither part contains "#[".
    assert validate.is_name("[x]")
    assert not validate.is_marker_text("!#")


# --- labels -------------------------------------------------------------------------------------

LABELS_OK: list[tuple[str, tuple[str, str]]] = [
    ("k=v", ("k", "v")),
    ("k=", ("k", "")),
    ("0=0", ("0", "0")),
    ("a.b-c_d=x", ("a.b-c_d", "x")),
    ("k=a=b", ("k", "a=b")),
    ("k==", ("k", "=")),
    ("ticket=KEY-12", ("ticket", "KEY-12")),
    ("url=https://host/x#frag@v+1,2", ("url", "https://host/x#frag@v+1,2")),
    ("k" * 64 + "=v", ("k" * 64, "v")),
    ("k=" + "v" * 256, ("k", "v" * 256)),
]
LABELS_BAD = [
    "",
    "k",
    "=v",
    "=",
    "K=v",
    "-k=v",
    ".k=v",
    "_k=v",
    "k" * 65 + "=v",
    "k=" + "v" * 257,
    "k=a b",
    "k=a;b",
    "k=a\nb",
    "k\n=v",
    "k=#[x]",
    "k=é",
    "k=$(id)",
    "k=`id`",
    "k=a'b",
    "k=a\\b",
    "k=a\x00",
    "ké=v",
]


@pytest.mark.parametrize(("arg", "pair"), LABELS_OK)
def test_label_ok(arg: str, pair: tuple[str, str]) -> None:
    assert validate.parse_label(arg) == pair
    assert schema_matches("label", arg)


@pytest.mark.parametrize("arg", LABELS_BAD)
def test_label_bad(arg: str) -> None:
    raises_usage(validate.parse_label, arg)
    assert not schema_matches("label", arg)


def test_labels_are_a_sorted_set_of_pairs() -> None:
    assert validate.parse_labels(["b=1", "a=2", "b=1", "a=10"]) == ["a=10", "a=2", "b=1"]


def test_labels_sort_by_code_point() -> None:
    assert validate.parse_labels(["a=b", "a=B", "a=#", "a.b=x"]) == ["a.b=x", "a=#", "a=B", "a=b"]


def test_label_limit_counts_unique_pairs() -> None:
    pairs = [f"k{i}=v" for i in range(32)]
    assert len(validate.parse_labels([*pairs, *pairs])) == 32
    validate.check_label_count(validate.parse_labels(pairs))
    raises_usage(validate.check_label_count, [*pairs, "x=y"])


# --- sessions and placements --------------------------------------------------------------------

SESSIONS_OK = ["termctl", "a", "A_b-9", "x" * 64, "-", "_"]
SESSIONS_BAD = ["", "x" * 65, "a.b", "a:b", "a b", "=a", "a;", "é", "#{x}", "a\n", "ａ"]


@pytest.mark.parametrize("value", SESSIONS_OK)
def test_session_name_ok(value: str) -> None:
    assert validate.check_session_name(value) == value
    assert schema_matches("session_name", value)


@pytest.mark.parametrize("value", SESSIONS_BAD)
def test_session_name_bad(value: str) -> None:
    raises_usage(validate.check_session_name, value)
    assert not schema_matches("session_name", value)


@pytest.mark.parametrize("value", ["a", "a.b:c d", "é", "x" * 1000, "#[x]", "\x1b"])
def test_session_filter_takes_any_non_empty_string(value: str) -> None:
    assert validate.check_session_filter(value) == value


def test_session_filter_rejects_empty() -> None:
    raises_usage(validate.check_session_filter, "")


PLACEMENTS_OK = ["default", "a", "0", "work_2-x", "a" * 64]
PLACEMENTS_BAD = ["", "a" * 65, "Default", "-a", "_a", "a.b", "a b", "a;", "é", "a\n"]


@pytest.mark.parametrize("value", PLACEMENTS_OK)
def test_placement_name_ok(value: str) -> None:
    assert validate.is_placement_name(value)
    assert validate.check_placement_name(value) == value
    assert schema_matches("placement_name", value)


@pytest.mark.parametrize("value", PLACEMENTS_BAD)
def test_placement_name_bad(value: str) -> None:
    assert not validate.is_placement_name(value)
    raises_usage(validate.check_placement_name, value)
    assert not schema_matches("placement_name", value)


# --- env ----------------------------------------------------------------------------------------

ENV_NAMES_OK = ["A", "_", "a", "PATH", "_X1", "lower_case", "X" * 300]
ENV_NAMES_BAD = ["", "1A", "A-B", "A.B", "A B", "É", "A\n", "A;", "$A", "ＡＢ"]


@pytest.mark.parametrize("value", ENV_NAMES_OK)
def test_env_name_ok(value: str) -> None:
    assert validate.is_env_name(value)
    assert schema_matches("env_name", value)


@pytest.mark.parametrize("value", ENV_NAMES_BAD)
def test_env_name_bad(value: str) -> None:
    assert not validate.is_env_name(value)
    raises_usage(validate.parse_env, [f"{value}=1"])


def test_env_values_are_split_at_the_first_equals_and_kept_verbatim() -> None:
    assert validate.parse_env(["A=1", "B=x=y", "C=", "D=$(id);", "E=é\tz"]) == {
        "A": "1",
        "B": "x=y",
        "C": "",
        "D": "$(id);",
        "E": "é\tz",
    }


@pytest.mark.parametrize("arg", ["A", "", "=1"])
def test_env_without_name_and_equals_is_rejected(arg: str) -> None:
    raises_usage(validate.parse_env, [arg])


def test_env_name_given_twice_is_rejected() -> None:
    raises_usage(validate.parse_env, ["A=1", "A=1"])


def test_env_names_are_case_sensitive() -> None:
    assert validate.parse_env(["a=1", "A=2"]) == {"a": "1", "A": "2"}


def test_env_limit_is_32() -> None:
    assert len(validate.parse_env([f"V{i}=x" for i in range(32)])) == 32
    raises_usage(validate.parse_env, [f"V{i}=x" for i in range(33)])


def test_env_error_names_the_variable_but_not_its_value() -> None:
    err = raises_usage(validate.parse_env, ["SECRET=hunter2", "SECRET=hunter3"])
    assert "SECRET" in err.message and "hunter" not in err.message


# --- durations ----------------------------------------------------------------------------------

DURATIONS_OK = [
    ("0", 0),
    ("0s", 0),
    ("0ms", 0),
    ("0m", 0),
    ("1", 1000),
    ("10s", 10_000),
    ("250ms", 250),
    ("600", 600_000),
    ("600s", 600_000),
    ("600000ms", 600_000),
    ("10m", 600_000),
    ("9m", 540_000),
]
DURATIONS_BAD = [
    "",
    "s",
    "ms",
    "00",
    "01s",
    "007ms",
    "601",
    "601s",
    "600001ms",
    "11m",
    "1h",
    "1.5s",
    "-1",
    "+1",
    " 1s",
    "1s ",
    "1s\n",
    "1 s",
    "1S",
    "1Ms",
    "١",  # Arabic-Indic digit one
    "５s",  # fullwidth digit
    "1_000ms",
    "9" * 50,
    "1e3ms",
    "0x10s",
]


@pytest.mark.parametrize(("value", "ms"), DURATIONS_OK)
def test_duration_ok(value: str, ms: int) -> None:
    assert validate.parse_duration(value) == ms
    assert schema_matches("duration", value)


@pytest.mark.parametrize("value", DURATIONS_BAD)
def test_duration_bad(value: str) -> None:
    assert validate.duration_ms(value) is None
    raises_usage(validate.parse_duration, value)
    assert not schema_matches("duration", value)


# --- actor --------------------------------------------------------------------------------------

ACTORS_OK = ["human:me", "auto:policy", "human:a.b_c@d-e", "auto:" + "x" * 64, "human:0"]
ACTORS_BAD = [
    "",
    "me",
    "human:",
    "human",
    "robot:x",
    "Human:x",
    "human:" + "x" * 65,
    "human:a b",
    "human:a:b",
    "human:é",
    "human:a\n",
    "human:a;",
    " human:a",
    "human:a/b",
]


@pytest.mark.parametrize("value", ACTORS_OK)
def test_actor_ok(value: str) -> None:
    assert validate.check_actor(value) == value
    assert schema_matches("actor", value)


@pytest.mark.parametrize("value", ACTORS_BAD)
def test_actor_bad(value: str) -> None:
    raises_usage(validate.check_actor, value)
    assert not schema_matches("actor", value)


def test_default_actor_on_a_tty_is_the_os_user() -> None:
    assert validate.default_actor(stdin_is_tty=True, user="alice") == ("human:alice", None)


@pytest.mark.parametrize("user", [None, "", "a b", "dom\\user", "é", "x" * 65, "a\x1b"])
def test_default_actor_falls_back_to_human_unknown_with_a_warning(user: str | None) -> None:
    actor, warning = validate.default_actor(stdin_is_tty=True, user=user)
    assert actor == "human:unknown"
    assert warning is not None
    assert "\x1b" not in warning


@pytest.mark.parametrize("user", ["alice", None, "a b"])
def test_default_actor_without_a_tty_is_auto_unknown(user: str | None) -> None:
    assert validate.default_actor(stdin_is_tty=False, user=user) == ("auto:unknown", None)


def test_resolve_actor_prefers_the_flag() -> None:
    gateway = FakeGateway(tty=True, user="alice")
    assert validate.resolve_actor("auto:cron", gateway) == ("auto:cron", None)


def test_resolve_actor_reads_tty_and_user_through_the_gateway() -> None:
    assert validate.resolve_actor(None, FakeGateway(tty=True, user="bob")) == ("human:bob", None)
    assert validate.resolve_actor(None, FakeGateway(tty=False, user="bob")) == (
        "auto:unknown",
        None,
    )
    actor, warning = validate.resolve_actor(None, FakeGateway(tty=True, user=None))
    assert actor == "human:unknown" and warning


def test_resolve_actor_checks_the_flag() -> None:
    raises_usage(validate.resolve_actor, "root", FakeGateway())


# --- decision ids -------------------------------------------------------------------------------

DECISION_IDS_OK = ["d", "D-1", "a.b_c:d@e/f-g", "x" * 128, "urn:x:1", "/", "."]
DECISION_IDS_BAD = ["", "x" * 129, "a b", "a;b", "a\n", "é", "a=b", "a#b", "$(id)", "a,b", "\x00"]


@pytest.mark.parametrize("value", DECISION_IDS_OK)
def test_decision_id_ok(value: str) -> None:
    assert validate.is_decision_id(value)
    assert validate.check_decision_ids([value]) == [value]


@pytest.mark.parametrize("value", DECISION_IDS_BAD)
def test_decision_id_bad(value: str) -> None:
    assert not validate.is_decision_id(value)
    raises_usage(validate.check_decision_ids, [value])


def test_decision_ids_keep_order_and_duplicates_up_to_32() -> None:
    ids = ["b", "a", "b"]
    assert validate.check_decision_ids(ids) == ids
    assert len(validate.check_decision_ids(["d"] * 32)) == 32
    raises_usage(validate.check_decision_ids, ["d"] * 33)


def test_decision_id_grammar_matches_schema() -> None:
    pattern = DEFS["decision_ids"]["items"]["pattern"]
    for value in DECISION_IDS_OK:
        assert search(pattern, value)
    for value in DECISION_IDS_BAD:
        assert not search(pattern, value)


# --- clients ------------------------------------------------------------------------------------

CLIENTS_OK = ["/dev/pts/3", "/dev/ttys001", "pts3", "/", "a" * 100, "/" + "a" * 100, "a.b_c-d"]
CLIENTS_BAD = ["", "a" * 101, "//" + "a" * 100, "/dev/pts/3;", "a b", "a:b", "é", "a\n", "$(id)"]


@pytest.mark.parametrize("value", CLIENTS_OK)
def test_client_ok(value: str) -> None:
    assert validate.check_client(value) == value
    assert schema_matches("client", value)


@pytest.mark.parametrize("value", CLIENTS_BAD)
def test_client_bad(value: str) -> None:
    raises_usage(validate.check_client, value)
    assert not schema_matches("client", value)


# --- sockets ------------------------------------------------------------------------------------

SOCKET_NAMES_OK = ["default", "a", "work.2", "a_b-c", "...", ".a", "a..", "x" * 64]
SOCKET_NAMES_BAD = ["", ".", "..", "x" * 65, "a b", "a;", "é", "a\n", "a:b", "$(id)", "#{x}"]
SOCKET_PATHS_OK = [
    "/tmp/tmux-1000/default",
    "/a",
    "/x/./y",
    "/x/../y",
    "/path with space/s",
    "/é/s",
    "/a;b/s",
    "/a/;b",
    "/a/b#[c",
]
SOCKET_PATHS_BAD = [
    "/",
    "//",
    "/a/",
    "/tmp/tmux-1000/",
    "/a;",
    "/a/b;",
    "/a\n/b",
    "/a/b\x00",
    "/a/b\x1b",
    "/a/\x9b",
    "/a/\udcff",
]
RELATIVE_PATHS = ["a/b", "./s", "../s", "dir/sock"]


@pytest.mark.parametrize("value", SOCKET_NAMES_OK + SOCKET_PATHS_OK + RELATIVE_PATHS)
def test_socket_flag_ok(value: str) -> None:
    assert validate.socket_problem(value, allow_relative=True) is None
    assert validate.check_socket_flag(value) == value


@pytest.mark.parametrize("value", SOCKET_NAMES_BAD + SOCKET_PATHS_BAD + ["a/", "a;/b;", "./"])
def test_socket_flag_bad(value: str) -> None:
    assert validate.socket_problem(value, allow_relative=True) is not None
    raises_usage(validate.check_socket_flag, value)


@pytest.mark.parametrize("value", SOCKET_NAMES_OK + SOCKET_PATHS_OK)
def test_socket_setting_ok(value: str) -> None:
    assert validate.socket_problem(value, allow_relative=False) is None


@pytest.mark.parametrize("value", SOCKET_NAMES_BAD + SOCKET_PATHS_BAD + RELATIVE_PATHS)
def test_socket_setting_bad(value: str) -> None:
    assert validate.socket_problem(value, allow_relative=False) is not None


def test_socket_is_a_name_unless_it_contains_a_slash() -> None:
    assert validate.is_socket_path("a/b") and validate.is_socket_path("/a")
    assert not validate.is_socket_path("a.b")


def test_socket_setting_agrees_with_schema() -> None:
    pattern = DEFS["socket"]["oneOf"][0]["pattern"]
    for value in SOCKET_NAMES_OK + SOCKET_PATHS_OK:
        assert search(pattern, value), value
    for value in SOCKET_NAMES_BAD + SOCKET_PATHS_BAD[:-1] + RELATIVE_PATHS:
        assert not search(pattern, value), value


# --- cwd ----------------------------------------------------------------------------------------

CWDS_BAD = ["/a\nb", "/a\tb", "/a\x1b", "/a\x7f", "/a\x85", "/a#[b", "/a/\udcff"]


@pytest.mark.parametrize("path", ["/", "/home/u", "/a b/é", "/a#b", "/a;", "/a/#(id)", "/[#"])
def test_cwd_text_ok(path: str) -> None:
    assert validate.cwd_problem(path) is None


@pytest.mark.parametrize("path", CWDS_BAD)
def test_cwd_text_bad(path: str) -> None:
    assert validate.cwd_problem(path) is not None


def test_resolve_cwd_defaults_to_the_callers_directory() -> None:
    gateway = FakeGateway(cwd="/home/u", dirs={"/home/u"})
    assert validate.resolve_cwd(None, gateway) == "/home/u"


def test_resolve_cwd_joins_a_relative_path_to_the_callers_directory() -> None:
    gateway = FakeGateway(cwd="/home/u", dirs={"/home/u/src"})
    assert validate.resolve_cwd("src", gateway) == "/home/u/src"


def test_resolve_cwd_keeps_an_absolute_path() -> None:
    gateway = FakeGateway(cwd="/home/u", dirs={"/srv"})
    assert validate.resolve_cwd("/srv", gateway) == "/srv"


def test_resolve_cwd_requires_an_existing_directory() -> None:
    raises_usage(validate.resolve_cwd, "/missing", FakeGateway(cwd="/", dirs=set()))


@pytest.mark.parametrize("path", CWDS_BAD)
def test_resolve_cwd_rejects_bad_text_before_touching_the_filesystem(path: str) -> None:
    gateway = FakeGateway(cwd="/", dirs={path})
    raises_usage(validate.resolve_cwd, path, gateway)
    assert gateway.dir_checks == []


def test_resolve_cwd_checks_the_callers_directory_text_too() -> None:
    gateway = FakeGateway(cwd="/w#[x]", dirs={"/w#[x]", "/w#[x]/sub"})
    raises_usage(validate.resolve_cwd, None, gateway)
    raises_usage(validate.resolve_cwd, "sub", gateway)


def test_resolve_cwd_rejects_an_empty_value() -> None:
    raises_usage(validate.resolve_cwd, "", FakeGateway(cwd="/", dirs={"/"}))


def test_resolve_cwd_unreadable_callers_directory_is_a_usage_error() -> None:
    raises_usage(validate.resolve_cwd, None, FakeGateway(cwd=None))


# --- error type ---------------------------------------------------------------------------------


def test_check_errors_are_usage_errors() -> None:
    err = raises_usage(validate.check_name, "")
    assert isinstance(err, TermctlError)
    assert err.envelope()["error"]["code"] == "usage"


@pytest.mark.parametrize("value", INJECTIONS)
@pytest.mark.parametrize(
    "check",
    [
        validate.check_marker_name,
        validate.check_placement_name,
        validate.check_session_name,
        validate.check_client,
        validate.check_socket_flag,
        lambda v: validate.check_actor(f"human:{v}"),
        lambda v: validate.check_decision_ids([v]),
        lambda v: validate.parse_env([f"{v}=1"]),
        lambda v: validate.parse_label(f"k={v}"),
        lambda v: validate.parse_label(f"{v}=1"),
    ],
)
def test_injection_looking_values_fail_every_ascii_grammar(
    check: Callable[[str], object], value: str
) -> None:
    raises_usage(check, value)
