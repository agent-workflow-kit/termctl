"""``termctl placements`` (interface.md "`placements`")."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl.cli import main

from .fakes import FakeClock, FakeGateway
from .schemas import validate

GOLDEN = Path(__file__).parent / "golden"
SCHEMA = "termctl/cli/placements.json"

CONFIG: dict[str, Any] = {
    "placements": {
        "default": {"session": "work"},
        "review": {"session": "reviews"},
        "build": {},
        "panes": {"target": "pane", "max_panes": 4},
    },
    "rules": [
        {"match": {"kind": "review", "repo": "app"}, "placement": "review"},
        {"match": {"kind": "build"}, "placement": "build"},
        {"match": {"kind": "split"}, "placement": "panes"},
        {"match": {}, "placement": "default"},
    ],
}


class Run:
    def __init__(self, argv: list[str], config: Any | None, tmp_path: Path) -> None:
        if config is None:
            environ = {"XDG_CONFIG_HOME": str(tmp_path / "no-config")}
        else:
            path = tmp_path / "config.yaml"
            path.write_text(
                config if isinstance(config, str) else yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )
            environ = {}
            argv = ["--config", str(path), *argv]
        self.gateway = FakeGateway()
        stdout, stderr = io.StringIO(), io.StringIO()
        self.code = main(
            argv,
            gateway=self.gateway,
            clock=FakeClock(),
            environ=environ,
            stdout=stdout,
            stderr=stderr,
        )
        self.stdout = stdout.getvalue()
        self.stderr = stderr.getvalue()

    def json(self) -> Any:
        return json.loads(self.stdout)


def golden(name: str) -> Any:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


# --- --json -------------------------------------------------------------------------------------


def test_built_in_json_golden(tmp_path: Path) -> None:
    run = Run(["placements", "--json"], None, tmp_path)
    assert (run.code, run.stderr) == (0, "")
    expected = golden("placements-built-in.json")
    validate(expected, SCHEMA)
    assert run.json() == expected


def test_configured_json_golden(tmp_path: Path) -> None:
    run = Run(["placements", "--json"], CONFIG, tmp_path)
    assert (run.code, run.stderr) == (0, "")
    expected = golden("placements-configured.json")
    validate(expected, SCHEMA)
    assert run.json() == expected


def test_label_match_json_golden(tmp_path: Path) -> None:
    argv = ["placements", "--json", "--label", "repo=app", "--label", "kind=review"]
    run = Run(argv, CONFIG, tmp_path)
    assert run.code == 0
    expected = golden("placements-match.json")
    validate(expected, SCHEMA)
    assert run.json() == expected


def test_match_is_null_without_labels(tmp_path: Path) -> None:
    assert Run(["placements", "--json"], CONFIG, tmp_path).json()["match"] is None


@pytest.mark.parametrize(
    ("labels", "rule", "name"),
    [
        (["kind=review", "repo=app"], 0, "review"),
        (["kind=review"], 3, "default"),
        (["kind=build", "kind=review", "repo=app"], 0, "review"),
        (["kind=build"], 1, "build"),
        (["other="], 3, "default"),
    ],
)
def test_label_reports_first_matching_rule(
    tmp_path: Path, labels: list[str], rule: int, name: str
) -> None:
    argv = ["placements", "--json"] + [f"--label={label}" for label in labels]
    run = Run(argv, CONFIG, tmp_path)
    assert run.code == 0
    data = run.json()
    validate(data, SCHEMA)
    assert data["match"] == {"labels": sorted(labels), "rule": rule, "placement": name}


def test_label_without_any_rule_reports_null_and_default(tmp_path: Path) -> None:
    run = Run(["placements", "--json", "--label", "kind=review"], None, tmp_path)
    data = run.json()
    validate(data, SCHEMA)
    assert data["match"] == {"labels": ["kind=review"], "rule": None, "placement": "default"}


def test_labels_are_sorted_and_deduplicated(tmp_path: Path) -> None:
    argv = ["placements", "--json", "--label", "b=1", "--label", "a=2", "--label", "b=1"]
    assert Run(argv, None, tmp_path).json()["match"]["labels"] == ["a=2", "b=1"]


def test_origin_values(tmp_path: Path) -> None:
    built_in = Run(["placements", "--json"], None, tmp_path).json()["placements"]
    assert built_in["default"]["origin"] == "built-in"
    config_only_other = {"placements": {"other": {}}}
    data = Run(["placements", "--json"], config_only_other, tmp_path).json()["placements"]
    assert data["default"]["origin"] == "built-in"
    assert data["other"]["origin"] == "config"
    data = Run(["placements", "--json"], CONFIG, tmp_path).json()["placements"]
    assert {name: value["origin"] for name, value in data.items()} == {
        "default": "config",
        "review": "config",
        "build": "config",
        "panes": "config",
    }


def test_listing_unsupported_placements_is_not_an_error(tmp_path: Path) -> None:
    run = Run(["placements", "--json", "--label", "kind=build"], CONFIG, tmp_path)
    assert run.code == 0
    assert run.json()["placements"]["panes"]["target"] == "pane"


# --- not_supported ------------------------------------------------------------------------------


def test_matched_pane_placement_is_not_supported(tmp_path: Path) -> None:
    run = Run(["placements", "--json", "--label", "kind=split"], CONFIG, tmp_path)
    assert run.code == 2
    expected = golden("error-placements-not-supported.json")
    validate(expected, "termctl/cli/error.json")
    assert run.json() == expected
    assert run.stderr == f"termctl: {expected['error']['message']}\n"


def test_matched_default_with_pane_fields_is_not_supported(tmp_path: Path) -> None:
    config = {"placements": {"default": {"layout": "tiled"}}}
    run = Run(["placements", "--label", "a=b"], config, tmp_path)
    assert run.code == 2
    assert run.stdout == ""
    assert "does not support" in run.stderr


def test_unsupported_default_without_label_is_listed(tmp_path: Path) -> None:
    config = {"placements": {"default": {"target": "pane"}}}
    run = Run(["placements", "--json"], config, tmp_path)
    assert run.code == 0
    validate(run.json(), SCHEMA)


# --- usage and config ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label", ["nokey", "=v", "Key=v", "k=v w", "k=\x1b", "k=" + "v" * 257, "k" * 65 + "=v"]
)
def test_invalid_label_is_a_usage_error(tmp_path: Path, label: str) -> None:
    run = Run(["placements", "--json", "--label", label], None, tmp_path)
    assert run.code == 2
    data = run.json()
    validate(data, "termctl/cli/error.json")
    assert data["error"]["code"] == "usage"


def test_more_than_32_labels_is_a_usage_error(tmp_path: Path) -> None:
    labels = [f"--label=k{index}=v" for index in range(33)]
    run = Run(["placements", "--json", *labels], None, tmp_path)
    assert run.code == 2
    assert run.json()["error"]["code"] == "usage"


def test_32_labels_are_accepted(tmp_path: Path) -> None:
    labels = [f"--label=k{index}=v" for index in range(32)]
    run = Run(["placements", "--json", *labels], None, tmp_path)
    assert run.code == 0
    validate(run.json(), SCHEMA)


def test_invalid_config_is_config_invalid(tmp_path: Path) -> None:
    config = {"rules": [{"match": {}, "placement": "missing"}]}
    run = Run(["placements", "--json"], config, tmp_path)
    assert run.code == 2
    assert run.json()["error"]["code"] == "config_invalid"


def test_placements_never_calls_tmux(tmp_path: Path) -> None:
    run = Run(["placements", "--label", "kind=review"], CONFIG, tmp_path)
    assert run.code == 0
    assert run.gateway.calls == []


# --- human output -------------------------------------------------------------------------------


def test_built_in_human_output(tmp_path: Path) -> None:
    run = Run(["placements"], None, tmp_path)
    assert (run.code, run.stderr) == (0, "")
    assert run.stdout == "placement default: session=termctl target=window (built-in)\n"


def test_configured_human_output(tmp_path: Path) -> None:
    run = Run(["placements", "--label", "kind=review", "--label", "repo=app"], CONFIG, tmp_path)
    assert run.code == 0
    assert run.stdout == (
        "placement default: session=work target=window (config)\n"
        "placement review: session=reviews target=window (config)\n"
        "placement build: session=work target=window (config)\n"
        "placement panes: session=work target=pane max_panes=4 (config)\n"
        "rule 0: kind=review repo=app -> review\n"
        "rule 1: kind=build -> build\n"
        "rule 2: kind=split -> panes\n"
        "rule 3: (any) -> default\n"
        "match kind=review repo=app: rule 0 -> review\n"
    )


def test_human_output_without_a_matching_rule(tmp_path: Path) -> None:
    run = Run(["placements", "--label", "a=b"], None, tmp_path)
    assert run.stdout.splitlines()[-1] == "match a=b: no rule -> default"


def test_human_output_quotes_free_text_fields(tmp_path: Path) -> None:
    config = {"placements": {"x": {"layout": "a b\x1bé", "group_by": None}}}
    run = Run(["placements"], config, tmp_path)
    assert run.code == 0
    assert run.stdout.splitlines()[1] == (
        'placement x: session=termctl target=window layout="a b\\u001bé" group_by=null (config)'
    )


def test_human_output_escapes_control_characters_in_free_text(tmp_path: Path) -> None:
    config = {"placements": {"x": {"group_by": "k\x85"}}}
    run = Run(["placements"], config, tmp_path)
    assert run.code == 0
    assert "\x85" not in run.stdout
    assert 'group_by="k\\x85"' in run.stdout


def test_json_output_keeps_free_text_fields_verbatim(tmp_path: Path) -> None:
    config = {"placements": {"x": {"layout": "a b\x1bé"}}}
    data = Run(["placements", "--json"], config, tmp_path).json()
    validate(data, SCHEMA)
    assert data["placements"]["x"]["layout"] == "a b\x1bé"
