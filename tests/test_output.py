from __future__ import annotations

import io
import json

import pytest

from termctl.errors import TermctlError
from termctl.output import Output, escape


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain text", "plain text"),
        ("\x00\x1f", "\\x00\\x1f"),
        ("a\nb\tc", "a\\x0ab\\x09c"),
        ("\x7f", "\\x7f"),
        ("\x80\x9b\x9f", "\\x80\\x9b\\x9f"),
        ("\x1b]0;x\x07", "\\x1b]0;x\\x07"),
        (" é ", " é "),
        ("back\\slash", "back\\slash"),
    ],
)
def test_escape(text: str, expected: str) -> None:
    assert escape(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\udcffb", "a\\xffb"),
        ("\udc80\udcc3\udca9", "\\x80\\xc3\\xa9"),
        ("\ud800", "\\ud800"),
        ("\udfff\udc7f", "\\udfff\\udc7f"),
    ],
)
def test_escape_lone_surrogates(text: str, expected: str) -> None:
    assert escape(text) == expected


HOSTILE = "\x1b[2J\x07\x9b\x7f"


def _error_output(json_mode: bool) -> tuple[str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    out = Output(stdout=stdout, stderr=stderr, json=json_mode)
    out.error(
        TermctlError(
            "vetoed",
            f"vetoed by hook gate: {HOSTILE}",
            hook=f"gate{HOSTILE}",
            hook_stderr=f"denied{HOSTILE}\nsecond line",
        )
    )
    return stdout.getvalue(), stderr.getvalue()


def test_human_error_output_never_carries_raw_controls_from_hook_text() -> None:
    stdout, stderr = _error_output(json_mode=False)
    assert stdout == ""
    assert stderr == "termctl: vetoed by hook gate: \\x1b[2J\\x07\\x9b\\x7f\n"


def test_json_error_output_keeps_hook_stderr_unchanged_and_json_encoded() -> None:
    stdout, stderr = _error_output(json_mode=True)
    assert "\x1b" not in stdout and "\\x1b" not in stdout
    error = json.loads(stdout)["error"]
    assert error["hook_stderr"] == f"denied{HOSTILE}\nsecond line"
    assert error["hook"] == f"gate{HOSTILE}"
    assert "\x1b" not in stderr
