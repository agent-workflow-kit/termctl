from __future__ import annotations

import pytest

from termctl.output import escape


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
