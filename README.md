# termctl

Places commands in terminal panes (tmux backend): open, rename, mark, focus, list and close panes by a stable id.

termctl is a Layer 1 (primitive) tool in the awkit set. It is fully standalone: it knows nothing about Claude or its callers, and callers say *what* to run while termctl's own config decides *where*.

## Status

Pre-1.0: every command is implemented, but the interface is not yet declared stable. What stands between this and 1.0 is listed in [1.0 readiness](docs/plan.md#10-readiness).

## Install

Requirements: Python 3.12 or newer and tmux 3.2 or newer on `PATH` (see [Platforms](#platforms)).

```sh
uv tool install git+https://github.com/agent-workflow-kit/termctl
termctl --version
```

## Quick start

Try it against a private tmux server, so nothing touches your own sessions. `--socket` names the server, and `TERMCTL_SOCKET` sets it for a whole shell. A name (as here) puts the socket in tmux's own per-user directory; a socket path must be in a directory only you (or root) can write, so `/tmp` itself is refused ([Server selection](docs/interface.md#server-selection)).

```sh
export TERMCTL_SOCKET=termctl-demo

# Open a command in a new window; the id (tc-...) is printed on stdout.
id=$(termctl open --name KEY-12 --label kind=review -- sleep 300)

termctl list                  # tc-... running termctl KEY-12
termctl mark "$id" attention  # the window is now named "🔔 KEY-12"
termctl unmark "$id"          # back to "KEY-12"
termctl --json show "$id"     # machine-readable record, including socket_path
termctl --json focus "$id"    # prints the attach command (outside tmux, on a terminal);
                              # exits 12 no_client (inside another tmux, or without a terminal)
termctl close "$id"           # refused (exit 20) while the command still runs

tmux -L termctl-demo kill-server   # stop the demo server
```

`close` refuses a pane whose command is still running; `close --force` is for a human (a terminal on stdin, or `--actor human:<user>`). Callers keep only the id (and `socket_path` from `--json`) and address every later command by it.

## Commands

[docs/interface.md](docs/interface.md#commands) is the single normative source for every flag, grammar, output and exit code; this table is a summary. `termctl <command> --help` shows the flags.

| Command | Purpose |
|---|---|
| `open [-- ARGV...]` | Start a command in a new pane (the only pane of a new window) and print its id. |
| `show <id>` | Print one managed pane. |
| `exists <id>` | Exit 0 if the pane exists, 10 if not. |
| `list` | List managed panes, filtered by label or session. |
| `rename <id> <name>` | Change the displayed name. |
| `mark <id> <marker>` / `unmark <id>` | Set or clear a named marker such as `attention`. |
| `label add\|remove <id> k=v...` | Edit a pane's labels. |
| `focus <id>` | Bring the pane into view: switch a tmux client to it, or attach here from a terminal outside tmux; with `--json`, returns the attach command instead of attaching when termctl would attach. |
| `close <id>` | Close the pane; refuses a running command unless forced. |
| `placements` | Print the effective placements. |

Every mutating command also takes `--dry-run`. Global flags (`--json`, `--socket`, `--config`, `--verbose`, `--quiet`, `--version`) are in [Global flags](docs/interface.md#global-flags), and the exit codes are summarized under [Exit codes](#exit-codes); `--json` output is described by the files in [schemas/](schemas/).

## Config

termctl reads `~/.config/termctl/config.yaml` (respecting `XDG_CONFIG_HOME`) plus `config.d/*.yaml`. [config.example.yaml](config.example.yaml) lists every key with its default; copy what you want to change. A small example that sends `kind=review` panes to their own session and defines a `done` marker next to the built-in `attention`:

```yaml
placements:
  reviews:
    session: reviews

rules:
  - match: {kind: review}
    placement: reviews

markers:
  done:
    text: "✓ "
```

Rules for config, placement and markers: [Config](docs/interface.md#config), [Placement](docs/interface.md#placement), [Markers](docs/interface.md#markers). Hooks (`pre-open`, `post-open`, `pre-close`, `post-close`) are described in [Hooks](docs/interface.md#hooks).

## Environment

A summary; [Environment](docs/interface.md#environment) is normative.

- `TERMCTL_<KEY>` sets a top-level scalar config key: `TERMCTL_SOCKET`, `TERMCTL_TMUX_BIN`, `TERMCTL_BACKEND`, `TERMCTL_KEEP_ON_EXIT`, `TERMCTL_CLOSE_GRACE`, `TERMCTL_MARKER_DISPLAY`. Precedence: flags > environment > config > built-in defaults.
- Hooks get `TERMCTL_HOOK`, `TERMCTL_ID` and `TERMCTL_OBJECT_TYPE` on top of the caller's full environment.
- termctl runs tmux with your environment filtered by `server_env_allow` (except the attach of `focus`, and hooks), so a server termctl starts, and the panes in it, do not get the rest of your environment (a server started by anyone else keeps its own environment). `SSH_AUTH_SOCK` is not in the default list; add it to `server_env_allow` to keep it.
- `TMUX_TMPDIR` decides where socket names live: if it is set in your shell profile but not under cron, the two use different tmux servers. Set it the same everywhere, or set `socket` in config.
- `--env VAR=value` reaches only the new pane and is not safe for secrets (values are briefly visible in process listings).
- termctl reads no credentials.

## Exit codes

A summary; [Exit codes](docs/interface.md#exit-codes) is normative.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected or internal error |
| 2 | usage error, invalid config, unsupported placement |
| 10 | id not found, no server running, client not found |
| 11 | a pre-hook vetoed |
| 12 | expected failure (`tmux_missing`, `tmux_too_old`, `server_unavailable`, `command_not_found`, `tag_failed`, `no_client`, `attach_failed`, `locked`, `state_changed`) |
| 13 | a pre-hook escalated |
| 20 | refused (`running`) |
| 21 | ambiguous id |

## Safety model

termctl keeps no database: what it knows about a pane is stored on the pane itself as tmux options, and it acts only on panes it opened. In summary:

- **Ids, not targets.** Callers address panes by the `tc-...` id; termctl never lists, renames, signals or closes a pane it did not open.
- **Server and state checks.** The tmux socket path, state directory, log, audit file and lock files are checked for ownership and permissions before use.
- **Filtered environment.** tmux gets an allow-listed environment (`server_env_allow`), so credentials in your shell are not added to a server termctl starts.
- **Human gates.** `close --force` and `--override-escalation` need a terminal or `--actor human:<user>`; a pre-hook veto always holds.
- **Audit.** Mutating runs are logged; every `close --force` that saw a running pane and every `--override-escalation` run are also audited, whatever the outcome, once the id is generated (`open`) or resolved (`close`) ([Audit and log](docs/interface.md#audit-and-log)).

Residual risks are real and documented, not hidden. The main ones: anyone who can reach the tmux socket has full control of the server (so the displayed name is untrusted), the human gates are advisory against a hostile caller, `--env` values are briefly visible in process listings (not safe for secrets), and `close --force` signals only the recorded process: children that ignore SIGHUP or left its process group survive, and on macOS (no pidfd) a reused PID can be signaled in a narrow race. The complete list is [Residual risks](docs/interface.md#residual-risks).

## Dependencies

- `tmux` 3.2 or newer: binding A (hard), behind an internal backend interface so another terminal backend can be added later.
- Python packages: `pyyaml`, `pydantic`.
- No other awkit tools.

## Platforms

- **Linux and WSL:** supported. CI runs the whole suite on tmux 3.2a (the floor, `make verify-floor`) and on the runner image's current tmux.
- **tmux:** 3.2 is the minimum. Some fields differ on 3.2 (for example `exit_signal` needs 3.3); see [Platform notes](docs/interface.md#platform-notes).
- **macOS:** best-effort. CI runs the suite in a `macos` job that is not a required check, and a manual checklist ([macOS checklist](docs/design.md#macos-checklist)) covers what only a real Mac shows. The checklist has not been run on a Mac yet; its results table is empty.
- **Windows:** not supported (use WSL).

## Development

```sh
uv sync
make verify         # lint, format check, type check, tests, markdown lint
make verify-floor TMUX_FLOOR=/path/to/tmux-3.2   # the suite on the tmux floor
```

Tests that need tmux start their own server on a private socket and never touch yours.

## Documentation

- [Interface](docs/interface.md) (normative), [design](docs/design.md), [plan](docs/plan.md)
- [awkit architecture](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md)
- [awkit tool design guide](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md)

## License

MIT, see LICENSE.
