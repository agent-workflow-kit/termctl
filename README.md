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

Try it against a private tmux server, so nothing touches your own sessions. `--socket` names the server, and `TERMCTL_SOCKET` sets it for a whole shell.

```sh
export TERMCTL_SOCKET=/tmp/termctl-demo.sock

# Open a command in a new window; the id (tc-...) is printed on stdout.
id=$(termctl open --name KEY-12 --label kind=review -- sleep 300)

termctl list                  # tc-... running termctl KEY-12
termctl mark "$id" attention  # the window is now named "🔔 KEY-12"
termctl unmark "$id"          # back to "KEY-12"
termctl --json show "$id"     # machine-readable record, including socket_path
termctl focus "$id"           # from a terminal: attach to the pane (exit 12 when no tmux client exists to switch)
termctl close "$id"           # refused (exit 20) while the command still runs

tmux -S "$TERMCTL_SOCKET" kill-server   # stop the demo server
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
| `focus <id>` | Bring the pane into view; `--json` returns the attach command instead of running it. |
| `close <id>` | Close the pane; refuses a running command unless forced. |
| `placements` | Print the effective placements. |

Every mutating command also takes `--dry-run`. Global flags (`--json`, `--socket`, `--config`, `--verbose`, `--quiet`, `--version`) and the exit codes are in [Global flags](docs/interface.md#global-flags) and [Exit codes](docs/interface.md#exit-codes); `--json` output is described by the files in [schemas/](schemas/).

## Config

termctl reads `~/.config/termctl/config.yaml` plus `config.d/*.yaml`. [config.example.yaml](config.example.yaml) lists every key with its default; copy what you want to change. A small example that sends `kind=review` panes to their own session and defines a marker:

```yaml
placements:
  reviews:
    session: reviews

rules:
  - match: {kind: review}
    placement: reviews

markers:
  attention:
    text: "🔔 "
```

Rules for config, placement and markers: [Config](docs/interface.md#config), [Placement](docs/interface.md#placement), [Markers](docs/interface.md#markers). Hooks (`pre-open`, `post-open`, `pre-close`, `post-close`) are described in [Hooks](docs/interface.md#hooks), and the environment variables in [Environment](docs/interface.md#environment).

## Safety model

termctl keeps no database: what it knows about a pane is stored on the pane itself as tmux options, and it acts only on panes it opened. In summary:

- **Ids, not targets.** Callers address panes by the `tc-...` id; termctl never lists, renames, signals or closes a pane it did not open.
- **Server and state checks.** The tmux socket path, state directory, log, audit file and lock files are checked for ownership and permissions before use.
- **Filtered environment.** tmux gets an allow-listed environment (`server_env_allow`), so credentials in your shell are not added to a server termctl starts.
- **Human gates.** `close --force` and `--override-escalation` need a terminal or `--actor human:<user>`; a pre-hook veto always holds.
- **Audit.** Mutating runs are logged; forced signals and overrides are also audited ([Audit and log](docs/interface.md#audit-and-log)).

Residual risks are real and documented, not hidden. The main ones: anyone who can reach the tmux socket has full control of the server (so the displayed name is untrusted), the human gates are advisory against a hostile caller, `--env` values are briefly visible in process listings (not safe for secrets), and `close --force` signals the recorded process by PID. The complete list is [Residual risks](docs/interface.md#residual-risks).

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
