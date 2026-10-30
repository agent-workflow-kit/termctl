"""termctl ids: generation, input normalization and prefix resolution
(interface.md "Identifiers")."""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from typing import Protocol

from .errors import TermctlError, UsageError
from .validate import show

PREFIX = "tc-"
FULL_LENGTH = 8
_BODY = re.compile(r"[a-z2-7]{4,8}")
_MAX_ATTEMPTS = 100


def generate(random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """A new id: 40 random bits as 8 lower-case base32 characters (RFC 4648 alphabet)."""
    return PREFIX + base64.b32encode(random_bytes(5)).decode("ascii").lower()


def generate_unique(
    taken: Container[str], random_bytes: Callable[[int], bytes] = secrets.token_bytes
) -> str:
    """A new id that no managed pane in ``taken`` carries."""
    for _ in range(_MAX_ATTEMPTS):
        candidate = generate(random_bytes)
        if candidate not in taken:
            return candidate
    raise TermctlError("internal", f"no free id after {_MAX_ATTEMPTS} attempts")


@dataclass(frozen=True)
class IdInput:
    """An id argument after normalization: ``body`` is 4 to 8 base32 characters."""

    raw: str
    body: str

    @property
    def full(self) -> bool:
        return len(self.body) == FULL_LENGTH

    @property
    def id(self) -> str | None:
        """The full id, or None for a prefix."""
        return PREFIX + self.body if self.full else None

    def matches(self, full_id: str) -> bool:
        return full_id.startswith(PREFIX + self.body)


def parse(raw: str) -> IdInput:
    """Normalize an id argument: lower-cased, ``tc-`` optional, 4 to 8 of ``[a-z2-7]``.

    Only ASCII input is lower-cased: some non-ASCII characters lower-case to ASCII letters
    (KELVIN SIGN to ``k``), so non-ASCII input is rejected first.
    """
    lowered = raw.lower() if raw.isascii() else None
    body = lowered.removeprefix(PREFIX) if lowered is not None else None
    if body is None or _BODY.fullmatch(body) is None:
        raise UsageError(
            f"invalid id {show(raw)}: expected tc- (optional) and 4 to 8 characters "
            "from a-z and 2-7"
        )
    return IdInput(raw=raw, body=body)


def require_full(raw: str) -> str:
    """A full id; a prefix is a usage error (``close --force``)."""
    parsed = parse(raw)
    if parsed.id is None:
        raise UsageError(f"{show(raw)} is a prefix; a full id is required here")
    return parsed.id


class Candidate(Protocol):
    """What resolution needs from a managed pane."""

    @property
    def id(self) -> str: ...

    @property
    def pane_id(self) -> str: ...

    @property
    def session(self) -> str: ...


def resolve[P: Candidate](id_input: IdInput, panes: Sequence[P]) -> P:
    """The one managed pane ``id_input`` names.

    No match exits 10 (``not_found``); several matches, including two panes carrying the same
    full id, exit 21 (``ambiguous``) with the candidates, and termctl acts on none of them.
    ``panes`` holds one entry per pane id (linked-window rows already merged).
    """
    matches = [pane for pane in panes if id_input.matches(pane.id)]
    if not matches:
        raise TermctlError("not_found", f"no managed pane matches {show(id_input.raw)}")
    if len(matches) > 1:
        raise TermctlError(
            "ambiguous",
            f"{show(id_input.raw)} matches {len(matches)} managed panes",
            candidates=[
                {"id": pane.id, "pane_id": pane.pane_id, "session": pane.session}
                for pane in matches
            ],
        )
    return matches[0]
