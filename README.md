# termctl

Terminal placement (tmux backend).

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive).

## What it does

- Opens sessions, windows and panes with a name, command and cwd; renames, focuses, closes, lists and checks for them.
- Knows nothing about Claude. Placement policy (which session, windows vs panes, naming) is passed in by the caller or configured. v1 placement: one shared session, a window per agent.
- Outside tmux (e.g. under cron) it targets the default tmux server socket (configurable) and creates the session if it is missing.
- `open` returns a stable window id; callers address windows by id, not by name.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

```text
  termctl open  (session/window/pane, name, command, cwd)
  termctl rename
  termctl focus
  termctl close
  termctl list
  termctl exists
```

## Dependencies

- `tmux`: binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/termctl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
