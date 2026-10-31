# termctl

Places commands in terminal panes (tmux backend): open, rename, mark, focus, list and close panes by a stable id.

## Status

Designed: interface 0.1.0-design ([docs/interface.md](docs/interface.md)); not yet implemented. Implementation plan: [docs/plan.md](docs/plan.md).

## Layer

L1 (primitive). Fully standalone; knows nothing about Claude or its callers.

## What it does

- `termctl open -- <argv...>` starts a command in a new pane (the only pane of a new tmux window) and prints a stable id (`tc-…`). termctl's config decides where it goes (session, window), optionally by matching the labels you pass.
- Callers address panes only by that id; each pane's window shows a friendly display name (e.g. `KEY-12`).
- Named markers (`termctl mark <id> attention`) show a status such as `🔔 KEY-12` and clear back to the plain name.
- `close` refuses panes whose command is still running unless you pass `--force`; it closes only the pane termctl opened.
- termctl keeps no database: everything it knows is stored on the tmux panes themselves.

## Commands

```text
termctl open [--name N] [--cwd DIR] [--env VAR=value]... [--label k=v]... [--placement P]
             [--session S] [--target window|pane] [--keep-on-exit never|failed|always] [--focus]
             [--dry-run] [--actor A] [--decision-id ID]... [--override-escalation]
             [--hook NAME=CMD]... [-- ARGV...]
termctl show <id>
termctl exists <id>
termctl list [--label k=v]... [--session S] [--dead]
termctl rename <id> <name> [--dry-run]
termctl mark <id> <marker> [--dry-run]
termctl unmark <id> [--dry-run]
termctl focus <id> [--client C] [--no-attach] [--dry-run]
termctl close <id> [--force] [--grace D] [--actor A] [--decision-id ID]... [--override-escalation]
              [--hook NAME=CMD]... [--dry-run]
termctl label add|remove <id> k=v... [--dry-run]
termctl placements [--label k=v]...
```

Global flags: `--json`, `--socket NAME|PATH`, `--config PATH`, `--verbose`, `--quiet`, `--version`, `-h`/`--help`. Full rules: [docs/interface.md](docs/interface.md).

## Exit codes

0 success; 1 internal error; 2 usage or invalid config; 10 not found (or no server, or `--client` not attached); 11 vetoed by a pre-hook; 12 expected failure (tmux missing or too old, server unavailable, command not found, no client to focus, lock busy, pane state changed during `close`, ...); 13 a pre-hook escalated; 20 refused (`running`); 21 ambiguous id. Details: [Exit codes](docs/interface.md#exit-codes).

## Config, hooks and environment

- Config: `~/.config/termctl/config.yaml` plus `config.d/*.yaml`; every key with its default in [config.example.yaml](config.example.yaml).
- Hooks: `pre-open`, `post-open`, `pre-close`, `post-close` ([Hooks](docs/interface.md#hooks)).
- Environment variables: `TERMCTL_SOCKET`, `TERMCTL_TMUX_BIN` and the other top-level scalar keys as `TERMCTL_<KEY>`. termctl reads no credentials.
- A pane gets the tmux server's environment, plus `PATH` from your filtered environment (`server_env_allow`), not the rest of yours. `SSH_AUTH_SOCK` is not passed by default; add it to `server_env_allow` to keep it. termctl's own attach and client switch never import your terminal's environment into a session, but a plain `tmux attach` does ([Environment](docs/interface.md#environment)). `--env VAR=value` adds variables to the new pane only but is **not safe for secrets** (values are briefly visible in process listings).
- If you set `TMUX_TMPDIR` in your shell profile, set it the same way for cron (or set `socket` in config), or the two environments will use different tmux servers.

## Dependencies

- `tmux` 3.2 or newer: binding A (hard), behind an internal backend interface so another terminal backend can be added later.
- Python packages: `pyyaml`, `pydantic`.
- No other awkit tools.

## Platforms

Linux and WSL. macOS best-effort: CI also runs the suite on macOS in a job that is not required, and a manual checklist (`docs/design.md`) is run before releases.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/termctl
```

## Documentation

- [Interface](docs/interface.md) (normative), [design](docs/design.md), [plan](docs/plan.md)
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
