"""Placement and rule resolution (interface.md "Placement" and "`placements`")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from termctl import placement
from termctl.config import Config, defaults, load
from termctl.errors import TermctlError


def load_config(tmp_path: Path, main: Any, *dropins: Any) -> Config:
    """Load ``main`` (and drop-ins, in order) as the config file given with ``--config``."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(main, sort_keys=False), encoding="utf-8")
    for index, data in enumerate(dropins):
        dropin = tmp_path / "config.d" / f"{index:02d}.yaml"
        dropin.parent.mkdir(exist_ok=True)
        dropin.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load({}, str(path))


# --- effective placements -----------------------------------------------------------------------


def test_built_in_default_only() -> None:
    assert placement.effective(defaults()) == {
        "default": {"session": "termctl", "target": "window"}
    }


def test_config_default_is_merged_over_the_built_in_field_by_field(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"default": {"session": "work"}}})
    assert placement.effective(cfg)["default"] == {"session": "work", "target": "window"}


def test_other_placement_inherits_missing_fields_from_effective_default(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path,
        {
            "placements": {
                "default": {"session": "work"},
                "review": {"target": "window"},
                "build": {"session": "builds"},
            }
        },
    )
    effective = placement.effective(cfg)
    assert effective["review"] == {"session": "work", "target": "window"}
    assert effective["build"] == {"session": "builds", "target": "window"}


def test_empty_placement_takes_every_field_from_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"empty": {}}})
    assert placement.effective(cfg)["empty"] == {"session": "termctl", "target": "window"}


def test_unsupported_default_fields_are_inherited(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path, {"placements": {"default": {"layout": "tiled"}, "other": {"session": "x"}}}
    )
    assert placement.effective(cfg)["other"] == {
        "session": "x",
        "target": "window",
        "layout": "tiled",
    }


def test_explicit_null_group_by_is_kept_not_inherited(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path,
        {"placements": {"default": {"group_by": "kind"}, "other": {"group_by": None}}},
    )
    effective = placement.effective(cfg)
    assert effective["default"]["group_by"] == "kind"
    assert effective["other"]["group_by"] is None


def test_default_comes_first_and_order_is_kept(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"zeta": {}, "alpha": {}}})
    assert list(placement.effective(cfg)) == ["default", "zeta", "alpha"]


def test_default_without_session_falls_back_to_the_built_in(tmp_path: Path) -> None:
    # A file that replaces `default` with a scalar and a drop-in that sets it again leave a
    # default without the built-in fields; the built-in still fills them.
    cfg = load_config(
        tmp_path,
        {"placements": {"default": "x"}},
        {"placements": {"default": {"layout": "tiled"}}},
    )
    assert placement.effective(cfg)["default"] == {
        "session": "termctl",
        "target": "window",
        "layout": "tiled",
    }


def test_effective_does_not_mutate_the_config(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"a": {}}})
    before = cfg.model_dump()
    placement.effective(cfg)["a"]["session"] = "changed"
    assert cfg.model_dump() == before
    assert placement.effective(cfg)["a"]["session"] == "termctl"


# --- origin -------------------------------------------------------------------------------------


def test_origin_of_the_built_in_default() -> None:
    assert placement.origin(defaults(), "default") == "built-in"


def test_origin_of_default_without_a_config_file(tmp_path: Path) -> None:
    cfg = load({"XDG_CONFIG_HOME": str(tmp_path / "none")})
    assert placement.origin(cfg, "default") == "built-in"


def test_origin_of_default_with_unrelated_config(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"tmux_bin": "tmux", "placements": {"other": {}}})
    assert placement.origin(cfg, "default") == "built-in"


def test_origin_of_default_with_an_empty_config_entry(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"default": {}}})
    assert placement.origin(cfg, "default") == "built-in"


@pytest.mark.parametrize(
    "fields",
    [
        {"session": "work"},
        {"session": "termctl"},  # equal to the built-in value, but set by config
        {"target": "window"},
        {"group_by": None},
        {"layout": "tiled"},
    ],
)
def test_origin_of_default_with_a_configured_field(tmp_path: Path, fields: Any) -> None:
    cfg = load_config(tmp_path, {"placements": {"default": fields}})
    assert placement.origin(cfg, "default") == "config"


def test_origin_of_default_set_only_in_a_dropin(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {}, {"placements": {"default": {"session": "work"}}})
    assert placement.origin(cfg, "default") == "config"


def test_origin_of_a_configured_placement(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"review": {"session": "r"}, "empty": {}}})
    assert placement.origin(cfg, "review") == "config"
    assert placement.origin(cfg, "empty") == "config"


# --- rule matching ------------------------------------------------------------------------------


def rules_config(tmp_path: Path, rules: list[dict[str, Any]], **placements: Any) -> Config:
    defined = {name: {} for rule in rules for name in [rule["placement"]]}
    defined.update(placements)
    return load_config(tmp_path, {"placements": defined, "rules": rules})


def test_no_rules_matches_nothing() -> None:
    assert placement.match_rule(defaults().rules, ["kind=review"]) is None


def test_rule_matches_when_every_pair_is_among_the_labels(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"kind": "review", "repo": "x"}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, ["kind=review", "other=1", "repo=x"]) == 0


def test_rule_does_not_match_when_a_pair_is_missing(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"kind": "review", "repo": "x"}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, ["kind=review"]) is None


def test_rule_value_must_match_exactly(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"kind": "review"}, "placement": "r"}])
    for labels in (["kind=Review"], ["kind=review2"], ["kind="], ["kinds=review"]):
        assert placement.match_rule(cfg.rules, labels) is None


def test_rule_matches_a_key_given_with_several_values(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"kind": "b"}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, ["kind=a", "kind=b"]) == 0


def test_label_value_containing_equals_is_split_at_the_first(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"k": "a=b"}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, ["k=a=b"]) == 0
    assert placement.match_rule(cfg.rules, ["k=a"]) is None


def test_empty_rule_value_matches_only_an_empty_label_value(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {"k": ""}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, ["k="]) == 0
    assert placement.match_rule(cfg.rules, ["k=v"]) is None


def test_empty_match_matches_everything(tmp_path: Path) -> None:
    cfg = rules_config(tmp_path, [{"match": {}, "placement": "r"}])
    assert placement.match_rule(cfg.rules, []) == 0
    assert placement.match_rule(cfg.rules, ["a=b"]) == 0


def test_first_matching_rule_wins(tmp_path: Path) -> None:
    cfg = rules_config(
        tmp_path,
        [
            {"match": {"kind": "build"}, "placement": "b"},
            {"match": {"kind": "review"}, "placement": "r1"},
            {"match": {"kind": "review"}, "placement": "r2"},
            {"match": {}, "placement": "any"},
        ],
    )
    assert placement.match_rule(cfg.rules, ["kind=review"]) == 1
    assert placement.match_rule(cfg.rules, ["kind=build", "kind=review"]) == 0
    assert placement.match_rule(cfg.rules, ["kind=other"]) == 3


# --- selection and support ----------------------------------------------------------------------


def test_select_without_a_match_uses_default() -> None:
    selected = placement.select(defaults(), ["a=b"])
    assert selected == placement.Selection(
        rule=None, name="default", fields={"session": "termctl", "target": "window"}
    )


def test_select_uses_the_matching_rule(tmp_path: Path) -> None:
    cfg = rules_config(
        tmp_path, [{"match": {"kind": "review"}, "placement": "r"}], r={"session": "rev"}
    )
    selected = placement.select(cfg, ["kind=review"])
    assert selected == placement.Selection(
        rule=0, name="r", fields={"session": "rev", "target": "window"}
    )


@pytest.mark.parametrize(
    ("fields", "unsupported"),
    [
        ({"target": "pane"}, ["target"]),
        ({"max_panes": 2}, ["max_panes"]),
        ({"layout": "tiled"}, ["layout"]),
        ({"group_by": None}, ["group_by"]),
        ({"group_by": "kind"}, ["group_by"]),
        (
            {"target": "pane", "max_panes": 2, "layout": "tiled", "group_by": "k"},
            ["target", "max_panes", "layout", "group_by"],
        ),
    ],
)
def test_unsupported_placement_is_not_supported(fields: Any, unsupported: list[str]) -> None:
    with pytest.raises(TermctlError) as caught:
        placement.check_supported("p", {"session": "s", "target": "window", **fields})
    err = caught.value
    assert (err.code, err.exit_code) == ("not_supported", 2)
    assert err.details == {"placement": "p", "fields": unsupported}
    assert "'p'" in err.message
    for field in unsupported:
        assert field in err.message


def test_window_placement_is_supported() -> None:
    placement.check_supported("p", {"session": "s", "target": "window"})


def test_select_rejects_an_inherited_unsupported_field(tmp_path: Path) -> None:
    cfg = rules_config(
        tmp_path,
        [{"match": {}, "placement": "r"}],
        default={"layout": "tiled"},
        r={"session": "x"},
    )
    with pytest.raises(TermctlError) as caught:
        placement.select(cfg, [])
    assert caught.value.code == "not_supported"


def test_select_rejects_a_default_with_target_pane(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, {"placements": {"default": {"target": "pane"}}})
    with pytest.raises(TermctlError) as caught:
        placement.select(cfg, [])
    assert caught.value.details == {"placement": "default", "fields": ["target"]}


def test_select_does_not_reject_unmatched_unsupported_placements(tmp_path: Path) -> None:
    cfg = rules_config(
        tmp_path,
        [{"match": {"kind": "x"}, "placement": "panes"}],
        panes={"target": "pane", "max_panes": 4},
    )
    assert placement.select(cfg, ["kind=y"]).name == "default"
