from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from jsonschema import ValidationError as SchemaError

from termctl import cli, config
from termctl.cli import Context, Result, main
from termctl.config import DEFAULTS, ENV_KEYS, Config, ConfigError, load

from .fakes import FakeClock, FakeGateway
from .schemas import SCHEMAS, validate

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden"


def write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory with an empty ``~/.config/termctl``."""
    (tmp_path / "home" / ".config" / "termctl").mkdir(parents=True)
    return tmp_path / "home"


@pytest.fixture
def main_file(home: Path) -> Path:
    return home / ".config" / "termctl" / "config.yaml"


@pytest.fixture
def dropins(home: Path) -> Path:
    return home / ".config" / "termctl" / "config.d"


def load_home(home: Path, **env: str) -> Config:
    return load({"HOME": str(home), **env})


@pytest.fixture
def no_access() -> Iterator[list[Path]]:
    """Paths a test makes mode 0; their modes are restored so tmp cleanup works."""
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o700)


def lock(no_access: list[Path], path: Path) -> None:
    path.chmod(0)
    no_access.append(path)


def load_error(environ: dict[str, str], explicit: str | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load(environ, explicit)
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    return info.value


# --- defaults -----------------------------------------------------------------------------------


def example() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))


def test_defaults_equal_config_example() -> None:
    assert DEFAULTS == example()


def test_schema_defaults_equal_defaults() -> None:
    properties = SCHEMAS["termctl/config.json"]["properties"]
    schema_defaults = {
        key: prop["default"] for key, prop in properties.items() if "default" in prop
    }
    assert schema_defaults == DEFAULTS


def test_config_example_matches_schema() -> None:
    validate(example(), "termctl/config.json")


def test_loading_nothing_gives_the_defaults(home: Path) -> None:
    assert load_home(home) == Config.model_validate(example())


def test_default_values() -> None:
    cfg = config.defaults()
    assert (cfg.backend, cfg.tmux_bin, cfg.socket) == ("tmux", "tmux", None)
    assert (cfg.keep_on_exit, cfg.close_grace, cfg.marker_display) == ("never", "10s", "name")
    assert cfg.markers["attention"].text == "🔔 " and cfg.markers["attention"].bell is False
    assert cfg.placements["default"].session == "termctl"
    assert cfg.placements["default"].target == "window"
    assert cfg.rules == [] and cfg.hooks.pre_open == [] and cfg.hooks.post_close == []
    assert "SSH_AUTH_SOCK" not in cfg.server_env_allow


def test_defaults_are_not_shared_between_loads(home: Path, main_file: Path) -> None:
    write(main_file, {"hooks": {"pre-open": [{"cmd": "a"}]}, "markers": {"x": {"text": "X"}}})
    load_home(home)
    assert DEFAULTS["hooks"] == {} and set(DEFAULTS["markers"]) == {"attention"}


# --- file locations -----------------------------------------------------------------------------


def test_reads_config_from_home(home: Path, main_file: Path) -> None:
    write(main_file, {"tmux_bin": "/opt/tmux"})
    assert load_home(home).tmux_bin == "/opt/tmux"


def test_xdg_config_home_wins_over_home(tmp_path: Path, home: Path, main_file: Path) -> None:
    write(main_file, {"tmux_bin": "from-home"})
    write(tmp_path / "xdg" / "termctl" / "config.yaml", {"tmux_bin": "from-xdg"})
    assert load_home(home, XDG_CONFIG_HOME=str(tmp_path / "xdg")).tmux_bin == "from-xdg"


@pytest.mark.parametrize("xdg", ["", "relative/dir"])
def test_empty_or_relative_xdg_config_home_is_ignored(
    home: Path, main_file: Path, xdg: str
) -> None:
    write(main_file, {"tmux_bin": "from-home"})
    assert load_home(home, XDG_CONFIG_HOME=xdg).tmux_bin == "from-home"


def test_config_flag_replaces_the_default_file(tmp_path: Path, home: Path, main_file: Path) -> None:
    write(main_file, {"tmux_bin": "from-home"})
    explicit = write(tmp_path / "elsewhere" / "my.yaml", {"tmux_bin": "from-flag"})
    assert load({"HOME": str(home)}, str(explicit)).tmux_bin == "from-flag"


def test_config_flag_reads_drop_ins_next_to_it(tmp_path: Path, home: Path, dropins: Path) -> None:
    write(dropins / "a.yaml", {"backend": "tmux", "tmux_bin": "home-dropin"})
    explicit = write(tmp_path / "elsewhere" / "my.yaml", {"tmux_bin": "from-flag"})
    write(tmp_path / "elsewhere" / "config.d" / "a.yaml", {"keep_on_exit": "always"})
    cfg = load({"HOME": str(home)}, str(explicit))
    assert (cfg.tmux_bin, cfg.keep_on_exit) == ("from-flag", "always")


def test_missing_default_file_is_not_an_error(home: Path, dropins: Path) -> None:
    write(dropins / "a.yaml", {"keep_on_exit": "failed"})
    assert load_home(home).keep_on_exit == "failed"


def test_missing_config_flag_file_is_an_error(tmp_path: Path) -> None:
    err = load_error({}, str(tmp_path / "absent.yaml"))
    assert "absent.yaml" in err.message


def test_missing_config_flag_file_names_the_file_in_details(tmp_path: Path) -> None:
    absent = tmp_path / "absent.yaml"
    assert load_error({}, str(absent)).details == {"file": str(absent)}


def test_dangling_symlink_as_the_default_file_is_an_error(home: Path, main_file: Path) -> None:
    main_file.symlink_to(home / "nowhere.yaml")
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(main_file)}


def test_unreadable_config_directory_is_an_error(
    home: Path, main_file: Path, no_access: list[Path]
) -> None:
    lock(no_access, main_file.parent)
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(main_file)}


def test_unreadable_drop_in_directory_is_an_error(
    home: Path, dropins: Path, no_access: list[Path]
) -> None:
    dropins.mkdir()
    lock(no_access, dropins)
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(dropins)}
    assert str(dropins) in err.message


def test_drop_in_directory_that_is_a_file_is_an_error(home: Path, dropins: Path) -> None:
    write(dropins, "tmux_bin: x\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(dropins)}


def test_home_falls_back_to_the_passwd_entry(
    monkeypatch: pytest.MonkeyPatch, home: Path, main_file: Path
) -> None:
    write(main_file, {"tmux_bin": "from-passwd"})

    class Entry:
        pw_dir = str(home)

    monkeypatch.setattr(config.pwd, "getpwuid", lambda uid: Entry())
    assert load({}).tmux_bin == "from-passwd"


def test_no_home_and_no_passwd_entry_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> Any:
        raise KeyError(uid)

    monkeypatch.setattr(config.pwd, "getpwuid", missing)
    err = load_error({})
    assert err.message == (
        "cannot determine the home directory; set HOME or XDG_CONFIG_HOME, or use --config"
    )


@pytest.mark.parametrize("home_value", ["", "relhome"])
def test_empty_or_relative_home_falls_back_to_the_passwd_entry(
    monkeypatch: pytest.MonkeyPatch, home: Path, main_file: Path, home_value: str
) -> None:
    write(main_file, {"tmux_bin": "from-passwd"})

    class Entry:
        pw_dir = str(home)

    monkeypatch.setattr(config.pwd, "getpwuid", lambda uid: Entry())
    assert load({"HOME": home_value}).tmux_bin == "from-passwd"


@pytest.mark.parametrize("pw_dir", ["", "relhome"])
def test_empty_or_relative_passwd_home_is_an_error(
    monkeypatch: pytest.MonkeyPatch, pw_dir: str
) -> None:
    entry = SimpleNamespace(pw_dir=pw_dir)
    monkeypatch.setattr(config.pwd, "getpwuid", lambda uid: entry)
    err = load_error({"HOME": "relhome"})
    assert err.message.startswith("cannot determine the home directory")


def test_dangling_symlink_above_the_default_file_is_an_error(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    (xdg / "termctl").symlink_to(tmp_path / "nowhere")
    err = load_error({"XDG_CONFIG_HOME": str(xdg)})
    assert err.details == {"file": str(xdg / "termctl")}
    assert "dangling symlink" in err.message


def test_dangling_drop_in_directory_symlink_is_an_error(
    tmp_path: Path, home: Path, dropins: Path
) -> None:
    dropins.symlink_to(tmp_path / "nowhere")
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(dropins)}
    assert "dangling symlink" in err.message


def test_fifo_drop_in_is_not_a_regular_file(home: Path, dropins: Path) -> None:
    dropins.mkdir()
    fifo = dropins / "a.yaml"
    os.mkfifo(fifo)
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(fifo)}
    assert "not a regular file" in err.message


@pytest.mark.parametrize("device", ["/dev/null", "/dev/zero"])
def test_main_file_symlink_to_a_device_is_not_a_regular_file(
    home: Path, main_file: Path, device: str
) -> None:
    main_file.symlink_to(device)
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(main_file)}
    assert "not a regular file" in err.message


def test_config_flag_naming_a_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    err = load_error({}, str(tmp_path))
    assert "not a regular file" in err.message


def test_regular_file_through_a_symlink_is_read(
    tmp_path: Path, home: Path, main_file: Path
) -> None:
    real = write(tmp_path / "real.yaml", {"tmux_bin": "linked"})
    main_file.symlink_to(real)
    assert load_home(home).tmux_bin == "linked"


def test_empty_file_means_no_settings(home: Path, main_file: Path) -> None:
    write(main_file, "# only a comment\n")
    assert load_home(home) == config.defaults()


@pytest.mark.parametrize("text", ["key: [unclosed\n", "- a list\n", "just a string\n"])
def test_unparsable_or_non_mapping_file_is_invalid(home: Path, main_file: Path, text: str) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert str(main_file) in err.message
    assert without_position(err.details) == {"file": str(main_file)}


def without_position(details: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (details or {}).items() if k not in ("line", "column")}


@pytest.mark.parametrize(
    ("text", "line", "column"),
    [
        ("tmux_bin: x\nkey: [unclosed\n", 3, 1),
        ("tmux_bin: x\nplacements: {a: !foo x}\n", 2, 17),
        ("tmux_bin: !!int abc\n", 1, 11),
        ("tmux_bin: !!float x\n", 1, 11),
        ("tmux_bin: !!bool yes\n", 1, 11),
        ("tmux_bin: a\ntmux_bin: b\n", 2, 1),
        ("markers:\n  a: &m {text: x}\n", 2, 6),
        ('tmux_bin: x\nfoo: "\\q"\n', 2, 8),
    ],
)
def test_yaml_errors_are_one_line_with_file_and_position(
    home: Path, main_file: Path, text: str, line: int, column: int
) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert "\n" not in err.message and "<unicode string>" not in err.message
    assert err.message.startswith(f"invalid config {main_file}: line {line}, column {column}: ")
    assert err.details is not None
    assert (err.details["file"], err.details["line"], err.details["column"]) == (
        str(main_file),
        line,
        column,
    )


def test_unparsable_drop_in_names_the_drop_in(home: Path, dropins: Path) -> None:
    bad = write(dropins / "b.yaml", ": : :\n")
    err = load_error({"HOME": str(home)})
    assert without_position(err.details) == {"file": str(bad)}


# --- YAML hazards -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("tmux_bin: a\ntmux_bin: b\n", "tmux_bin"),
        ("placements:\n  a: {session: x, session: y}\n", "placements.a.session"),
    ],
)
def test_duplicate_keys_are_invalid(home: Path, main_file: Path, text: str, key: str) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert without_position(err.details) == {"file": str(main_file), "key": key}
    assert str(main_file) in err.message and key in err.message


def test_duplicate_keys_in_a_drop_in_name_the_drop_in(home: Path, dropins: Path) -> None:
    bad = write(dropins / "a.yaml", "hooks:\n  pre-open: []\n  pre-open: []\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(bad), "key": "hooks.pre-open", "line": 3, "column": 3}


@pytest.mark.parametrize(
    "text",
    [
        "markers:\n  a: &m {text: x}\n  b: *m\n",
        "placements:\n  a: &p {session: x}\n",
        "placements:\n  a: {<<: {session: x}}\n",
    ],
)
def test_anchors_aliases_and_merge_keys_are_invalid(home: Path, main_file: Path, text: str) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert without_position(err.details) == {"file": str(main_file)}
    assert str(main_file) in err.message


def test_alias_bomb_fails_fast(home: Path, main_file: Path) -> None:
    lines = ["a0: &a0 {x: 1, y: 1}"]
    lines += [f"a{i}: &a{i} {{x: *a{i - 1}, y: *a{i - 1}}}" for i in range(1, 40)]
    lines += ["placements: {p: {layout: *a39}}"]
    write(main_file, "\n".join(lines) + "\n")
    start = time.monotonic()
    err = load_error({"HOME": str(home)})
    assert time.monotonic() - start < 0.5
    assert without_position(err.details) == {"file": str(main_file)}


@pytest.mark.parametrize(
    ("text", "key", "shown"),
    [
        ("placements: {true: {}}\n", "placements.true", "true"),
        ("markers: {1: {text: x}}\n", "markers.1", "1"),
        ("markers: {0x1F: {text: x}}\n", "markers.0x1F", "0x1F"),
        ("markers: {1.5: {text: x}}\n", "markers.1.5", "1.5"),
        ("null: 1\n", "null", "null"),
    ],
)
def test_non_string_keys_are_invalid(
    home: Path, main_file: Path, text: str, key: str, shown: str
) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert without_position(err.details) == {"file": str(main_file), "key": key}
    assert shown in err.message and "quote" in err.message


def test_deep_nesting_is_invalid(home: Path, main_file: Path) -> None:
    write(main_file, "a: " + "[" * 100_000 + "]" * 100_000 + "\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"file": str(main_file)}


def test_unprintable_character_is_one_line_naming_the_file(home: Path, main_file: Path) -> None:
    write(main_file, "tmux_bin: a\x01\n")
    err = load_error({"HOME": str(home)})
    assert err.message == (
        f"invalid config {main_file}: position 11: unacceptable character #x0001:"
        " special characters are not allowed"
    )
    assert err.details == {"file": str(main_file)}


# --- YAML 1.2 core schema -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("17", 17),
        ("017", 17),
        ("0o17", 15),
        ("0x1F", 31),
        ("+5", 5),
        ("1e1", 10),
        ("5.0", 5),
    ],
)
def test_integers_are_read_as_yaml_1_2(
    home: Path, main_file: Path, text: str, expected: int
) -> None:
    write(main_file, f"hooks:\n  pre-open: [{{cmd: x, timeout: {text}}}]\n")
    assert load_home(home).hooks.pre_open[0].timeout == expected


@pytest.mark.parametrize("text", ["1:30", "1_000", "0b11", ".inf", "-.inf", ".nan", "0o8"])
def test_yaml_1_1_numbers_are_not_integers(home: Path, main_file: Path, text: str) -> None:
    write(main_file, f"hooks:\n  pre-open: [{{cmd: x, timeout: {text}}}]\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "hooks.pre-open[0].timeout"}


@pytest.mark.parametrize(
    "text", ["yes", "no", "on", "off", "y", "n", "1:30", "2024-01-01", "1_000", "<<"]
)
def test_yaml_1_1_scalars_are_strings(home: Path, main_file: Path, text: str) -> None:
    write(main_file, f"tmux_bin: {text}\n")
    assert load_home(home).tmux_bin == text


@pytest.mark.parametrize(("text", "expected"), [("true", True), ("False", False), ("TRUE", True)])
def test_booleans_are_true_and_false(
    home: Path, main_file: Path, text: str, expected: bool
) -> None:
    write(main_file, f"markers: {{m: {{text: x, bell: {text}}}}}\n")
    assert load_home(home).markers["m"].bell is expected


@pytest.mark.parametrize("text", ["yes", "no", "on", "off"])
def test_yaml_1_1_booleans_are_rejected_with_a_hint(home: Path, main_file: Path, text: str) -> None:
    write(main_file, f"markers: {{m: {{text: x, bell: {text}}}}}\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "markers.m.bell"}
    assert "true or false" in err.message


@pytest.mark.parametrize("text", ["~", "null", "Null", "NULL", ""])
def test_nulls_are_yaml_1_2(home: Path, main_file: Path, text: str) -> None:
    write(main_file, f"placements: {{a: {{layout: {text}}}}}\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "placements.a.layout"}


def test_yaml_1_1_boolean_words_are_string_keys(home: Path, main_file: Path) -> None:
    write(
        main_file,
        "placements: {no: {session: x}, on: {}}\nrules: [{match: {yes: n}, placement: no}]\n",
    )
    cfg = load_home(home)
    assert set(cfg.placements) == {"default", "no", "on"}
    assert cfg.rules[0].match == {"yes": "n"}


def test_yaml_1_1_boolean_word_as_a_top_level_key_is_unknown(home: Path, main_file: Path) -> None:
    write(main_file, "yes: 1\n")
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "yes"}


# --- Unicode ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('placements: {a: {layout: "\\ud800"}}\n', "placements.a.layout"),
        ('placements: {a: {group_by: "x\\udcff"}}\n', "placements.a.group_by"),
        ('hooks: {post-open: [{cmd: "\\ud800"}]}\n', "hooks.post-open[0].cmd"),
        ('tmux_bin: "\\udcff"\n', "tmux_bin"),
        ('placements: {"a\\udcff": {}}\n', "placements.a\udcff"),
    ],
)
def test_lone_surrogates_in_config_are_rejected(
    home: Path, main_file: Path, text: str, key: str
) -> None:
    write(main_file, text)
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": key}
    assert err.message.startswith(f"invalid config: {key}: ")
    assert "valid Unicode" in err.message


@pytest.mark.parametrize("var", ["TERMCTL_TMUX_BIN", "TERMCTL_CLOSE_GRACE", "TERMCTL_BACKEND"])
def test_lone_surrogates_in_variables_are_rejected(home: Path, var: str) -> None:
    err = load_error({"HOME": str(home), var: "\udcff"})
    assert err.details == {"variable": var}
    assert err.message == f"invalid {var}: must be valid Unicode text"


# --- drop-ins and merging -----------------------------------------------------------------------


def test_drop_ins_apply_in_code_point_order(home: Path, main_file: Path, dropins: Path) -> None:
    write(main_file, {"tmux_bin": "main"})
    write(dropins / "2.yaml", {"tmux_bin": "two"})
    write(dropins / "10.yaml", {"tmux_bin": "ten", "keep_on_exit": "always"})
    write(dropins / "b.yaml", {"marker_display": "name"})
    write(dropins / "B.yaml", {"marker_display": "option", "close_grace": "5s"})
    cfg = load_home(home)
    assert (cfg.tmux_bin, cfg.keep_on_exit) == ("two", "always")
    assert (cfg.marker_display, cfg.close_grace) == ("name", "5s")


def test_only_visible_yaml_drop_ins_are_read(home: Path, dropins: Path) -> None:
    write(dropins / "a.yml", {"tmux_bin": "yml"})
    write(dropins / "a.yaml.bak", {"tmux_bin": "bak"})
    write(dropins / ".hidden.yaml", {"tmux_bin": "hidden"})
    assert load_home(home).tmux_bin == "tmux"


def test_maps_merge_at_every_depth(home: Path, main_file: Path, dropins: Path) -> None:
    write(
        main_file,
        {
            "placements": {"default": {"session": "main"}, "reviews": {"session": "rev"}},
            "markers": {"done": {"text": "ok "}},
        },
    )
    write(dropins / "a.yaml", {"placements": {"reviews": {"layout": "tiled"}}})
    write(dropins / "b.yaml", {"markers": {"done": {"bell": True}}})
    cfg = load_home(home)
    assert cfg.placements["default"].session == "main"
    assert cfg.placements["default"].target == "window"  # built-in field kept
    assert (cfg.placements["reviews"].session, cfg.placements["reviews"].layout) == (
        "rev",
        "tiled",
    )
    assert cfg.markers["attention"].text == "🔔 "  # built-in marker kept
    assert (cfg.markers["done"].text, cfg.markers["done"].bell) == ("ok ", True)


def test_hook_lists_are_appended(home: Path, main_file: Path, dropins: Path) -> None:
    write(main_file, {"hooks": {"pre-open": [{"cmd": "main"}], "post-close": [{"cmd": "pc"}]}})
    write(dropins / "a.yaml", {"hooks": {"pre-open": [{"cmd": "a", "timeout": 5}]}})
    write(dropins / "b.yaml", {"hooks": {"pre-open": [{"cmd": "b"}], "pre-close": [{"cmd": "c"}]}})
    hooks = load_home(home).hooks
    assert [(h.cmd, h.timeout) for h in hooks.pre_open] == [("main", 30), ("a", 5), ("b", 30)]
    assert [h.cmd for h in hooks.pre_close] == ["c"]
    assert [h.cmd for h in hooks.post_close] == ["pc"]


def test_other_lists_are_replaced(home: Path, main_file: Path, dropins: Path) -> None:
    write(
        main_file,
        {
            "server_env_allow": ["PATH", "HOME"],
            "placements": {"a": {}, "b": {}},
            "rules": [{"match": {"kind": "x"}, "placement": "a"}],
        },
    )
    write(
        dropins / "a.yaml",
        {"server_env_allow": ["SSH_AUTH_SOCK"], "rules": [{"match": {}, "placement": "b"}]},
    )
    cfg = load_home(home)
    assert cfg.server_env_allow == ["SSH_AUTH_SOCK"]
    assert [(r.match, r.placement) for r in cfg.rules] == [({}, "b")]


def test_a_drop_in_scalar_replaces_a_map(home: Path, main_file: Path, dropins: Path) -> None:
    write(main_file, {"socket": "work"})
    write(dropins / "a.yaml", {"socket": None})
    assert load_home(home).socket is None


def test_merge_does_not_modify_its_inputs() -> None:
    base = {"hooks": {"pre-open": [1]}, "m": {"a": 1}}
    over = {"hooks": {"pre-open": [2]}, "m": {"b": 2}}
    assert config.merge(base, over) == {"hooks": {"pre-open": [1, 2]}, "m": {"a": 1, "b": 2}}
    assert base == {"hooks": {"pre-open": [1]}, "m": {"a": 1}}


def test_only_lists_under_hooks_are_appended() -> None:
    merged = config.merge({"x": {"pre-open": [1]}}, {"x": {"pre-open": [2]}})
    assert merged == {"x": {"pre-open": [2]}}


# --- TERMCTL_ variables -------------------------------------------------------------------------

ENV_VALUES: dict[str, tuple[str, str, Any]] = {
    # key: (config value, environment value, expected)
    "socket": ("work", "/run/tmux/sock", "/run/tmux/sock"),
    "tmux_bin": ("tmux", "/opt/bin/tmux", "/opt/bin/tmux"),
    "backend": ("tmux", "tmux", "tmux"),
    "keep_on_exit": ("failed", "always", "always"),
    "close_grace": ("5s", "250ms", "250ms"),
    "marker_display": ("name", "option", "option"),
}


def test_env_keys_are_the_top_level_scalars() -> None:
    assert set(ENV_KEYS) == set(ENV_VALUES)


@pytest.mark.parametrize("key", sorted(ENV_VALUES))
def test_environment_overrides_config(home: Path, main_file: Path, key: str) -> None:
    from_config, from_env, expected = ENV_VALUES[key]
    write(main_file, {key: from_config})
    assert getattr(load_home(home), key) == from_config
    cfg = load_home(home, **{f"TERMCTL_{key.upper()}": from_env})
    assert getattr(cfg, key) == expected


@pytest.mark.parametrize("socket", ["work", "/run/tmux/other"])
def test_termctl_socket_name_or_path_overrides_config_socket(
    home: Path, main_file: Path, socket: str
) -> None:
    write(main_file, {"socket": "/run/tmux/sock"})
    assert load_home(home, TERMCTL_SOCKET=socket).socket == socket


def test_environment_overrides_drop_ins(home: Path, dropins: Path) -> None:
    write(dropins / "a.yaml", {"keep_on_exit": "failed"})
    assert load_home(home, TERMCTL_KEEP_ON_EXIT="never").keep_on_exit == "never"


@pytest.mark.parametrize(
    "var",
    [
        "TERMCTL_UNKNOWN",
        "TERMCTL_HOOK",
        "TERMCTL_ID",
        "TERMCTL_OBJECT_TYPE",
        "TERMCTL_SERVER_ENV_ALLOW",
        "TERMCTL_MARKERS",
        "TERMCTL_RULES",
        "TERMCTL_socket",
    ],
)
def test_other_termctl_variables_are_ignored(home: Path, var: str) -> None:
    assert load_home(home, **{var: "{not: [valid"}) == config.defaults()


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("TERMCTL_SOCKET", "rel/path"),
        ("TERMCTL_SOCKET", ".."),
        ("TERMCTL_SOCKET", ""),
        ("TERMCTL_TMUX_BIN", ""),
        ("TERMCTL_BACKEND", "screen"),
        ("TERMCTL_KEEP_ON_EXIT", "sometimes"),
        ("TERMCTL_CLOSE_GRACE", "11m"),
        ("TERMCTL_CLOSE_GRACE", "5 s"),
        ("TERMCTL_MARKER_DISPLAY", "title"),
    ],
)
def test_invalid_variable_is_config_invalid_naming_it(home: Path, var: str, value: str) -> None:
    err = load_error({"HOME": str(home), var: value})
    assert err.message.startswith(f"invalid {var}: ")
    assert err.details == {"variable": var}


def test_invalid_variable_is_reported_even_if_config_sets_the_key(
    home: Path, main_file: Path
) -> None:
    write(main_file, {"marker_display": "option"})
    err = load_error({"HOME": str(home), "TERMCTL_MARKER_DISPLAY": "x"})
    assert err.details == {"variable": "TERMCTL_MARKER_DISPLAY"}


# --- load-time checks ---------------------------------------------------------------------------


def test_rule_naming_an_undefined_placement_is_rejected(home: Path, main_file: Path) -> None:
    write(
        main_file,
        {
            "placements": {"reviews": {}},
            "rules": [
                {"match": {"kind": "review"}, "placement": "reviews"},
                {"match": {}, "placement": "nope"},
            ],
        },
    )
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "rules[1].placement"}
    assert "rules[1].placement" in err.message and "nope" in err.message


def test_rule_may_name_the_built_in_default(home: Path, main_file: Path) -> None:
    write(main_file, {"rules": [{"match": {}, "placement": "default"}]})
    assert load_home(home).rules[0].placement == "default"


def test_rule_may_name_a_placement_from_a_drop_in(
    home: Path, main_file: Path, dropins: Path
) -> None:
    write(main_file, {"rules": [{"match": {"k": "v"}, "placement": "late"}]})
    write(dropins / "a.yaml", {"placements": {"late": {"session": "late"}}})
    assert load_home(home).rules[0].placement == "late"


@pytest.mark.parametrize("hook", ["pre-open", "pre-close"])
@pytest.mark.parametrize("value", [True, False])
def test_async_on_a_pre_hook_is_rejected(
    home: Path, main_file: Path, dropins: Path, hook: str, value: bool
) -> None:
    write(main_file, {"hooks": {hook: [{"cmd": "ok"}]}})
    write(dropins / "a.yaml", {"hooks": {hook: [{"cmd": "x", "async": value}]}})
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": f"hooks.{hook}[1].async"}
    assert f"hooks.{hook}[1].async" in err.message


def test_hook_keys_index_the_merged_list(home: Path, main_file: Path, dropins: Path) -> None:
    write(main_file, {"hooks": {"pre-open": [{"cmd": "main"}]}})
    write(dropins / "a.yaml", {"hooks": {"pre-open": [{"cmd": "dropin", "async": True}]}})
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "hooks.pre-open[1].async"}


@pytest.mark.parametrize("hook", ["post-open", "post-close"])
def test_async_on_a_post_hook_is_allowed(home: Path, main_file: Path, hook: str) -> None:
    write(main_file, {"hooks": {hook: [{"cmd": "x", "async": True}]}})
    entries = getattr(load_home(home).hooks, hook.replace("-", "_"))
    assert entries[0].async_ is True


@pytest.mark.parametrize(
    "socket",
    ["work", "a.b_c-D", "x" * 64, "/tmp/tmux-1000/default", "/s p a c e/sock", "/a/b;c"],
)
def test_socket_name_or_absolute_path_is_accepted(home: Path, main_file: Path, socket: str) -> None:
    write(main_file, {"socket": socket})
    assert load_home(home).socket == socket


@pytest.mark.parametrize(
    "socket",
    [
        "",
        ".",
        "..",
        "x" * 65,
        "bad name",
        "a;",
        "rel/path",
        "./sock",
        "/",
        "/tmp/dir/",
        "/tmp/sock;",
        "/tmp/a\tb",
        "/tmp/a\x85b",
        "/" + "a" * 100,
        "/tmp/a,b",
        "/tmp/.",
        "/tmp/..",
        "/tmp/a\ud800b",
        "x\udcff",
    ],
)
def test_socket_must_be_a_name_or_absolute_path(home: Path, main_file: Path, socket: str) -> None:
    write(main_file, {"socket": socket})
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "socket"}
    with pytest.raises(SchemaError):
        validate({"socket": socket}, "termctl/config.json")


def test_socket_path_limit_counts_bytes(home: Path, main_file: Path) -> None:
    # 99 and 101 bytes in 50 and 51 code points: the limit is bytes (interface.md "Server
    # selection"); the schema's maxLength counts code points and only approximates it.
    write(main_file, {"socket": "/" + "\u00e9" * 49})
    assert load_home(home).socket == "/" + "\u00e9" * 49
    write(main_file, {"socket": "/" + "\u00e9" * 50})
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": "socket"}
    assert "100 bytes" in err.message


@pytest.mark.parametrize("socket", ["/tmp/a,b", "/tmp/..", "rel/sock", "/tmp/\udcff"])
def test_termctl_socket_uses_the_config_socket_grammar(home: Path, socket: str) -> None:
    err = load_error({"HOME": str(home), "TERMCTL_SOCKET": socket})
    assert err.details == {"variable": "TERMCTL_SOCKET"}
    assert err.message.startswith("invalid TERMCTL_SOCKET: ")


def test_unsupported_placement_fields_load(home: Path, main_file: Path) -> None:
    write(
        main_file,
        {
            "placements": {
                "default": {"target": "pane", "max_panes": 4},
                "grid": {"target": "pane", "max_panes": 2, "layout": "tiled", "group_by": "kind"},
                "flat": {"group_by": None},
            },
            "rules": [{"match": {"kind": "x"}, "placement": "grid"}],
        },
    )
    cfg = load_home(home)
    assert (cfg.placements["default"].target, cfg.placements["default"].max_panes) == ("pane", 4)
    grid = cfg.placements["grid"]
    assert (grid.target, grid.max_panes, grid.layout, grid.group_by) == ("pane", 2, "tiled", "kind")
    assert "group_by" in cfg.placements["flat"].model_fields_set


def test_placement_fields_left_out_stay_unset(home: Path, main_file: Path) -> None:
    write(main_file, {"placements": {"reviews": {"session": "rev"}}})
    reviews = load_home(home).placements["reviews"]
    assert (reviews.target, reviews.max_panes, reviews.layout, reviews.group_by) == (
        None,
        None,
        None,
        None,
    )
    assert reviews.model_fields_set == {"session"}


# --- schema agreement ---------------------------------------------------------------------------

VALID: list[dict[str, Any]] = [
    {"close_grace": "0"},
    {"close_grace": "600"},
    {"close_grace": "600000ms"},
    {"close_grace": "10m"},
    {"server_env_allow": []},
    {"server_env_allow": ["PATH", "LC_*", "_X1"]},
    {"markers": {"a-b_1": {"text": "x" * 16, "bell": True}}},
    {"markers": {"done": {"text": "a#b"}}},
    {"markers": {"m": {"text": "\U0001f514\u2028x"}}},
    {"placements": {"a": {"session": "S_1-x"}}},
    {"rules": [{"match": {"kind.sub-x": "A-z0.9:/#@+=,-", "e": ""}, "placement": "default"}]},
    {"hooks": {"pre-open": [{"cmd": "x", "timeout": 1}]}},
    {"hooks": {"post-open": [{"cmd": "x", "timeout": 1.0}]}},
    {"placements": {"a": {"max_panes": 2.0}}},
]

INVALID: list[tuple[dict[str, Any], str]] = [
    ({"unknown": 1}, "unknown"),
    ({"backend": "screen"}, "backend"),
    ({"tmux_bin": ""}, "tmux_bin"),
    ({"tmux_bin": 5}, "tmux_bin"),
    ({"keep_on_exit": "sometimes"}, "keep_on_exit"),
    ({"close_grace": 10}, "close_grace"),
    ({"close_grace": "601s"}, "close_grace"),
    ({"close_grace": "010s"}, "close_grace"),
    ({"close_grace": "11m"}, "close_grace"),
    ({"close_grace": "1h"}, "close_grace"),
    ({"marker_display": "both"}, "marker_display"),
    ({"server_env_allow": "PATH"}, "server_env_allow"),
    ({"server_env_allow": ["1PATH"]}, "server_env_allow[0]"),
    ({"server_env_allow": ["PATH", "A*B"]}, "server_env_allow[1]"),
    ({"markers": {"Bad": {"text": "x"}}}, "markers.Bad"),
    ({"markers": {"m": {}}}, "markers.m.text"),
    ({"markers": {"m": {"text": ""}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "x" * 17}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "ends#"}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "a#[b"}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "a\nb"}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "a\ud800"}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "\udc80b"}}}, "markers.m.text"),
    ({"markers": {"m": {"text": "x", "bell": "yes"}}}, "markers.m.bell"),
    ({"markers": {"m": {"text": "x", "colour": 1}}}, "markers.m.colour"),
    ({"placements": {"-bad": {}}}, "placements.-bad"),
    ({"placements": {"a": {"session": "has space"}}}, "placements.a.session"),
    ({"placements": {"a": {"session": None}}}, "placements.a.session"),
    ({"placements": {"a": {"target": "tab"}}}, "placements.a.target"),
    ({"placements": {"a": {"target": None}}}, "placements.a.target"),
    ({"placements": {"a": {"max_panes": 0}}}, "placements.a.max_panes"),
    ({"placements": {"a": {"max_panes": True}}}, "placements.a.max_panes"),
    ({"placements": {"a": {"max_panes": 2.5}}}, "placements.a.max_panes"),
    ({"placements": {"a": {"layout": 3}}}, "placements.a.layout"),
    ({"placements": {"a": {"extra": 1}}}, "placements.a.extra"),
    ({"placements": None}, "placements"),
    ({"rules": [{"placement": "default"}]}, "rules[0].match"),
    ({"rules": [{"match": {}}]}, "rules[0].placement"),
    ({"rules": [{"match": {"Key": "v"}, "placement": "default"}]}, "rules[0].match.Key"),
    ({"rules": [{"match": {"k": "a b"}, "placement": "default"}]}, "rules[0].match.k"),
    ({"rules": [{"match": {"k": 1}, "placement": "default"}]}, "rules[0].match.k"),
    ({"hooks": {"on-open": []}}, "hooks.on-open"),
    ({"hooks": {"pre-open": [{}]}}, "hooks.pre-open[0].cmd"),
    ({"hooks": {"pre-open": [{"cmd": ""}]}}, "hooks.pre-open[0].cmd"),
    ({"hooks": {"post-open": [{"cmd": "x", "timeout": 0}]}}, "hooks.post-open[0].timeout"),
    ({"hooks": {"pre-open": [{"cmd": "x", "timeout": 1.5}]}}, "hooks.pre-open[0].timeout"),
    ({"hooks": {"post-open": [{"cmd": "x", "async": "true"}]}}, "hooks.post-open[0].async"),
    ({"hooks": {"post-close": [{"cmd": "x", "env": {}}]}}, "hooks.post-close[0].env"),
    ({"hooks": {"post-open": [{"cmd": "x", "async_": True}]}}, "hooks.post-open[0].async_"),
    ({"hooks": {"pre_open": []}}, "hooks.pre_open"),
    ({"hooks": {"post_close": []}}, "hooks.post_close"),
]


@pytest.mark.parametrize("data", VALID)
def test_valid_configs_agree_with_schema(home: Path, main_file: Path, data: dict[str, Any]) -> None:
    validate(data, "termctl/config.json")
    write(main_file, data)
    load_home(home)


def test_integral_floats_load_as_integers(home: Path, main_file: Path) -> None:
    write(
        main_file,
        {
            "hooks": {"pre-open": [{"cmd": "x", "timeout": 5.0}]},
            "placements": {"a": {"max_panes": 2.0}},
        },
    )
    cfg = load_home(home)
    assert cfg.hooks.pre_open[0].timeout == 5 and type(cfg.hooks.pre_open[0].timeout) is int
    assert cfg.placements["a"].max_panes == 2 and type(cfg.placements["a"].max_panes) is int


@pytest.mark.parametrize(("data", "key"), INVALID)
def test_invalid_configs_agree_with_schema(
    home: Path, main_file: Path, data: dict[str, Any], key: str
) -> None:
    with pytest.raises(SchemaError):
        validate(data, "termctl/config.json")
    write(main_file, data)
    err = load_error({"HOME": str(home)})
    assert err.details == {"key": key}
    assert err.message.startswith(f"invalid config: {key}: ")


# --- the CLI ------------------------------------------------------------------------------------


def run(
    argv: list[str], environ: dict[str, str], gateway: FakeGateway | None = None
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(
        argv,
        gateway=gateway or FakeGateway(),
        clock=FakeClock(),
        environ=environ,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_cli_loads_config_into_the_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[Context] = []

    def capture(args: Any, ctx: Context) -> Result:
        seen.append(ctx)
        return Result({}, "")

    monkeypatch.setattr(cli, "run_command", capture)
    explicit = write(tmp_path / "c.yaml", {"keep_on_exit": "failed"})
    code, _, _ = run(
        ["placements", "--config", str(explicit)], {"TERMCTL_MARKER_DISPLAY": "option"}
    )
    assert code == 0
    assert (seen[0].config.keep_on_exit, seen[0].config.marker_display) == ("failed", "option")


def test_context_defaults_to_the_built_in_config() -> None:
    assert config.defaults() == Config.model_validate(DEFAULTS)


@pytest.mark.parametrize("command", [["placements"], ["show", "tc-abcdefgh"], ["open"]])
def test_cli_invalid_config_exits_2_before_tmux(tmp_path: Path, command: list[str]) -> None:
    explicit = write(tmp_path / "c.yaml", {"rules": [{"match": {}, "placement": "nope"}]})
    gateway = FakeGateway()
    code, stdout, stderr = run(["--json", "--config", str(explicit), *command], {}, gateway)
    assert code == 2
    assert gateway.calls == []
    envelope = json.loads(stdout)
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "config_invalid"
    assert stderr.startswith("termctl: invalid config: rules[0].placement: ")


@pytest.mark.parametrize(
    ("golden", "files", "environ"),
    [
        (
            "error-config-invalid-rule.json",
            {"rules": [{"match": {"kind": "review"}, "placement": "reviews"}]},
            {},
        ),
        (
            "error-config-invalid-pre-hook-async.json",
            {"hooks": {"pre-close": [{"cmd": "gate", "async": True}]}},
            {},
        ),
        ("error-config-invalid-variable.json", {}, {"TERMCTL_CLOSE_GRACE": "11m"}),
    ],
)
def test_config_invalid_golden(
    tmp_path: Path, golden: str, files: dict[str, Any], environ: dict[str, str]
) -> None:
    explicit = write(tmp_path / "c.yaml", files)
    expected = json.loads((GOLDEN / golden).read_text(encoding="utf-8"))
    validate(expected, "termctl/cli/error.json")
    code, stdout, _ = run(["--json", "--config", str(explicit), "placements"], environ)
    assert code == 2
    assert json.loads(stdout) == expected


def test_cli_undecodable_termctl_socket_is_reported_escaped(tmp_path: Path) -> None:
    # Bytes that are not UTF-8 reach os.environ as lone surrogates (surrogateescape).
    explicit = write(tmp_path / "c.yaml", {})
    code, stdout, stderr = run(
        ["--json", "--config", str(explicit), "placements"], {"TERMCTL_SOCKET": "/tmp/s\udcff"}
    )
    assert code == 2
    envelope = json.loads(stdout)
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "config_invalid"
    assert envelope["error"]["details"] == {"variable": "TERMCTL_SOCKET"}
    assert stderr.startswith("termctl: invalid TERMCTL_SOCKET: ")
    stderr.encode()  # no lone surrogate reaches the stream


@pytest.mark.parametrize("argv", [["--config", ""], ["--config="]])
def test_cli_empty_config_flag_is_a_usage_error(argv: list[str]) -> None:
    gateway = FakeGateway()
    code, stdout, _ = run(["--json", *argv, "placements"], {}, gateway)
    assert code == 2
    envelope = json.loads(stdout)
    validate(envelope, "termctl/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert envelope["error"]["message"] == "--config must not be empty"
