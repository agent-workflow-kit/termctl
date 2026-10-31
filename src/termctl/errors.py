"""Error codes, their exit codes and the ``{"error": {...}}`` envelope (interface.md "Errors")."""

from __future__ import annotations

from typing import Any

EXIT_CODES: dict[str, int] = {
    "usage": 2,
    "config_invalid": 2,
    "not_supported": 2,
    "not_found": 10,
    "no_server": 10,
    "client_not_found": 10,
    "vetoed": 11,
    "tmux_missing": 12,
    "tmux_too_old": 12,
    "server_unavailable": 12,
    "command_not_found": 12,
    "tag_failed": 12,
    "no_client": 12,
    "attach_failed": 12,
    "locked": 12,
    "state_changed": 12,
    "escalated": 13,
    "refused": 20,
    "ambiguous": 21,
    "internal": 1,
}


class TermctlError(Exception):
    """An expected failure with an error code from interface.md "Errors"."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        hook: str | None = None,
        hook_stderr: str | None = None,
        decision_ids: list[str] | None = None,
        candidates: list[dict[str, str]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.hook = hook
        self.hook_stderr = hook_stderr
        self.decision_ids = decision_ids
        self.candidates = candidates
        self.details = details

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def envelope(self) -> dict[str, Any]:
        """The ``--json`` error output (``schemas/cli/error.json``); absent fields are omitted."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        optional = {
            "reason": self.reason,
            "hook": self.hook,
            "hook_stderr": self.hook_stderr,
            "decision_ids": self.decision_ids or None,
            "candidates": self.candidates,
            "details": self.details,
        }
        error.update({key: value for key, value in optional.items() if value is not None})
        return {"error": error}


class UsageError(TermctlError):
    """A usage error (exit 2). ``usage`` is the usage line shown to humans, if any."""

    def __init__(self, message: str, *, usage: str | None = None) -> None:
        super().__init__("usage", message)
        self.usage = usage
