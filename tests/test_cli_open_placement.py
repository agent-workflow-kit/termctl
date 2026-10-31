"""Placement resolution for ``termctl open`` (interface.md "Placement", "Open order" step 2)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import cli, placement
from termctl.cli import Context, Result, main

from .fakes import FakeClock, FakeGateway
from .schemas import validate

CONFIG: dict[str, Any] = {
    "placements": {
        "default": {"session": "work"},
        "review": {"session": "reviews"},
        "build": {},
        "panes": {"target": "pane", "max_panes": 4},
    },
    "rules": [
        {"match": {"kind": "review"}, "placement": "review"},
        {"match": {"kind": "split"}, "placement": "panes"},
        {"match": {}, "placement": "build"},
    ],
}


class Run:
    """One ``main`` call of ``open`` with ``config`` (None: no config file); ``selection`` is
    what step 5 receives, None when it was not reached."""

    def __init__(
        self,
        argv: list[str],
        config: Any | None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if config is None:
            environ = {"XDG_CONFIG_HOME": str(tmp_path / "no-config")}
        else:
            path = tmp_path / "config.yaml"
            text = config if isinstance(config, str) else yaml.safe_dump(config, sort_keys=False)
            path.write_text(text, encoding="utf-8")
            environ = {}
            argv = ["--config", str(path), *argv]
        self.selection: placement.Selection | None = None

        def capture(args: Any, ctx: Context) -> Result:
            self.selection = ctx.selection
            return Result({}, "")

        monkeypatch.setattr(cli, "run_command", capture)
        self.gateway = FakeGateway()
        stdout, stderr = io.StringIO(), io.StringIO()
        self.code = main(
            ["--json", *argv],
            gateway=self.gateway,
            clock=FakeClock(),
            environ=environ,
            stdout=stdout,
            stderr=stderr,
        )
        self.stdout = stdout.getvalue()
        self.stderr = stderr.getvalue()

    def error(self) -> Any:
        data = json.loads(self.stdout)
        validate(data, "termctl/cli/error.json")
        return data["error"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["open"], (2, "build", {"session": "work", "target": "window"})),
        (
            ["open", "--label", "kind=review"],
            (0, "review", {"session": "reviews", "target": "window"}),
        ),
        (["open", "--label", "kind=x"], (2, "build", {"session": "work", "target": "window"})),
        (
            ["open", "--label", "kind=review", "--placement", "build"],
            (None, "build", {"session": "work", "target": "window"}),
        ),
        (
            ["open", "--placement", "default", "--session", "mine"],
            (None, "default", {"session": "mine", "target": "window"}),
        ),
        (
            ["open", "--label", "kind=review", "--session", "s", "--target", "window"],
            (0, "review", {"session": "s", "target": "window"}),
        ),
    ],
)
def test_open_resolves_the_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected: tuple[int | None, str, dict[str, str]],
) -> None:
    run = Run(argv, CONFIG, tmp_path, monkeypatch)
    assert (run.code, run.stderr) == (0, "")
    rule, name, fields = expected
    assert run.selection == placement.Selection(rule=rule, name=name, fields=fields)


def test_open_without_config_uses_the_built_in_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(["open"], None, tmp_path, monkeypatch)
    assert run.selection == placement.Selection(
        rule=None, name="default", fields={"session": "termctl", "target": "window"}
    )


def test_open_with_an_undefined_placement_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Run(["open", "--placement", "nope"], CONFIG, tmp_path, monkeypatch)
    assert run.code == 2
    assert run.error() == {"code": "usage", "message": "undefined placement 'nope'"}
    assert run.selection is None
    assert run.gateway.calls == []


@pytest.mark.parametrize(
    ("argv", "name", "fields"),
    [
        (["open", "--label", "kind=split"], "panes", ["target", "max_panes"]),
        (["open", "--placement", "panes"], "panes", ["target", "max_panes"]),
        (["open", "--placement", "panes", "--target", "window"], "panes", ["max_panes"]),
        (["open", "--target", "pane"], "build", ["target"]),
        (["open", "--label", "kind=review", "--target", "pane"], "review", ["target"]),
    ],
)
def test_open_with_an_unsupported_placement_is_not_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    name: str,
    fields: list[str],
) -> None:
    run = Run(argv, CONFIG, tmp_path, monkeypatch)
    assert run.code == 2
    error = run.error()
    assert error["code"] == "not_supported"
    assert error["details"] == {"placement": name, "fields": fields}
    assert run.selection is None
    assert run.gateway.calls == []


def test_invalid_placement_name_fails_before_the_config_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Step 1 grammar: a name that could never be defined is rejected without a config lookup.
    run = Run(["open", "--placement", "Bad Name"], "not: [valid", tmp_path, monkeypatch)
    assert run.code == 2
    assert run.error()["code"] == "usage"
    assert "invalid placement name" in run.error()["message"]


def test_unsupported_placement_in_config_does_not_affect_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"placements": {"default": {"target": "pane"}}}
    run = Run(["list"], config, tmp_path, monkeypatch)
    assert run.code == 0
    assert run.selection is None
