"""Id generation, input normalization and prefix resolution (interface.md "Identifiers")."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from termctl import ids
from termctl.errors import TermctlError, UsageError

from .schemas import SCHEMAS, validate

GOLDEN = Path(__file__).parent / "golden"
ID_PATTERN = SCHEMAS["termctl/defs.json"]["$defs"]["id"]["pattern"]


@dataclass(frozen=True)
class Pane:
    id: str
    pane_id: str
    session: str


# --- generation ---------------------------------------------------------------------------------


def test_generated_ids_match_the_grammar() -> None:
    for _ in range(200):
        assert re.search(ID_PATTERN, ids.generate())


def test_generation_is_base32_of_40_random_bits() -> None:
    assert ids.generate(lambda n: b"\x00" * n) == "tc-aaaaaaaa"
    assert ids.generate(lambda n: b"\xff" * n) == "tc-77777777"
    # RFC 4648 base32 of b"hello" is NBSWY3DP.
    assert ids.generate(lambda n: b"hello"[:n]) == "tc-nbswy3dp"


def test_generation_asks_for_five_bytes() -> None:
    sizes: list[int] = []

    def random_bytes(n: int) -> bytes:
        sizes.append(n)
        return bytes(n)

    ids.generate(random_bytes)
    assert sizes == [5]


def test_generate_unique_skips_taken_ids() -> None:
    draws = iter([b"\x00" * 5, b"\x00" * 5, b"\xff" * 5])
    assert ids.generate_unique({"tc-aaaaaaaa"}, lambda n: next(draws)) == "tc-77777777"


def test_generate_unique_gives_up_rather_than_looping_forever() -> None:
    with pytest.raises(TermctlError) as info:
        ids.generate_unique({"tc-aaaaaaaa"}, lambda n: bytes(n))
    assert info.value.code == "internal"


# --- input normalization ------------------------------------------------------------------------

INPUTS_OK: list[tuple[str, str, bool]] = [
    ("tc-abcdefgh", "abcdefgh", True),
    ("abcdefgh", "abcdefgh", True),
    ("TC-ABCDEFGH", "abcdefgh", True),
    ("Tc-AbCdEfGh", "abcdefgh", True),
    ("ABCDEFGH", "abcdefgh", True),
    ("tc-abcd", "abcd", False),
    ("abcd", "abcd", False),
    ("ABCD", "abcd", False),
    ("tc-a2b3c4d", "a2b3c4d", False),
    ("2345", "2345", False),
    ("tcab", "tcab", False),  # no "tc-": the letters t and c are part of the id
    ("tc-tcab", "tcab", False),
]
INPUTS_BAD = [
    "",
    "tc-",
    "tc",
    "a",
    "abc",  # too short
    "tc-abc",
    "abcdefghi",  # longer than a full id
    "tc-abcdefghi",
    "tc-abcd1",  # 1 is not base32
    "abcd0",
    "abcd8",
    "abcd9",
    "tc_abcd",
    "tc--abcd",
    "-tc-abcd",
    " abcd",
    "abcd ",
    "abcd\n",
    "ab\x00cd",
    "ab\x1bcd",
    "%1",
    "@1",
    "abcd;",
    "tc-abcd*",
    "abc.",
    "\u212aabcd",  # KELVIN SIGN lower-cases to "k"
    "tc-\u0130bcd",  # I WITH DOT ABOVE lower-cases to "i" + combining dot
    "\uff41\uff42\uff43\uff44",  # fullwidth abcd
    "tc-" + "a" * 5000,
]


@pytest.mark.parametrize(("raw", "body", "full"), INPUTS_OK)
def test_parse_accepts_and_lower_cases(raw: str, body: str, full: bool) -> None:
    parsed = ids.parse(raw)
    assert (parsed.body, parsed.full) == (body, full)
    assert parsed.raw == raw


@pytest.mark.parametrize("raw", INPUTS_BAD)
def test_parse_rejects_with_exit_2(raw: str) -> None:
    with pytest.raises(UsageError) as info:
        ids.parse(raw)
    assert info.value.exit_code == 2
    assert "\x1b" not in info.value.message and "\x00" not in info.value.message
    assert len(info.value.message) < 300


def test_full_id_property() -> None:
    assert ids.parse("ABCDEFGH").id == "tc-abcdefgh"
    assert ids.parse("abcd").id is None


def test_require_full_accepts_only_full_ids() -> None:
    assert ids.require_full("TC-ABCDEFGH") == "tc-abcdefgh"
    with pytest.raises(UsageError):
        ids.require_full("tc-abcd")
    with pytest.raises(UsageError):
        ids.require_full("bad!")


# --- prefix resolution --------------------------------------------------------------------------

PANES = [
    Pane("tc-abcdefgh", "%1", "termctl"),
    Pane("tc-abcdxxxx", "%2", "work"),
    Pane("tc-zzzz2222", "%3", "termctl"),
]


def test_resolve_full_id() -> None:
    assert ids.resolve(ids.parse("tc-abcdefgh"), PANES) is PANES[0]


def test_resolve_unique_prefix() -> None:
    assert ids.resolve(ids.parse("abcde"), PANES) is PANES[0]
    assert ids.resolve(ids.parse("ZZZZ"), PANES) is PANES[2]


def test_resolve_matches_only_from_the_start() -> None:
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("2222"), PANES)
    assert (info.value.code, info.value.exit_code) == ("not_found", 10)


def test_resolve_no_match_exits_10() -> None:
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("qqqq"), PANES)
    assert info.value.exit_code == 10
    with pytest.raises(TermctlError):
        ids.resolve(ids.parse("qqqq"), [])


def test_resolve_ambiguous_prefix_exits_21_with_candidates() -> None:
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("abcd"), PANES)
    err = info.value
    assert (err.code, err.exit_code) == ("ambiguous", 21)
    assert err.candidates == [
        {"id": "tc-abcdefgh", "pane_id": "%1", "session": "termctl"},
        {"id": "tc-abcdxxxx", "pane_id": "%2", "session": "work"},
    ]
    validate(err.envelope(), "termctl/cli/error.json")


def test_resolve_duplicate_full_id_is_ambiguous_and_acts_on_neither() -> None:
    copied = [*PANES, Pane("tc-abcdefgh", "%7", "other")]
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("tc-abcdefgh"), copied)
    assert info.value.exit_code == 21
    assert info.value.candidates == [
        {"id": "tc-abcdefgh", "pane_id": "%1", "session": "termctl"},
        {"id": "tc-abcdefgh", "pane_id": "%7", "session": "other"},
    ]


def test_ambiguous_golden() -> None:
    expected = json.loads((GOLDEN / "error-ambiguous-id.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("abcd"), PANES)
    assert info.value.envelope() == expected


def test_not_found_golden() -> None:
    expected = json.loads((GOLDEN / "error-id-not-found.json").read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    with pytest.raises(TermctlError) as info:
        ids.resolve(ids.parse("QQQQ"), PANES)
    assert info.value.envelope() == expected
