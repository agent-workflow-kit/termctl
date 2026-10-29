# termctl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Terminal placement (tmux backend). Layer 1 (primitive). Must work fully standalone (architecture §2, C2).

## Responsibilities

From architecture §5.1:

Terminal placement. tmux backend.

- Commands: `open` (session/window/pane, name, command, cwd), `rename`, `focus`, `close`, `list`, `exists`.
- Uses: `tmux`.
- Notes: Knows nothing about Claude. Placement policy (which session, windows vs panes, naming) is passed in by the caller or configured. v1 placement: one shared session, a window per agent (QRT-4). Outside tmux (e.g. under cron) it targets the default tmux server socket (configurable) and creates the session if it is missing.

## Uses

- `tmux`: binding: TBD (A/B/C), decided in this tool's design session

## Architecture sections to read

Section numbers refer to `../awkit-docs/architecture.md` (v1.4).

- §2: C4 (termctl needs macOS only if interactive use is wanted) and C5 (tmux only for now; keep the terminal boundary clean so another backend can be added).
- §6: conventions summary.
- §9.3: window status prefix: callers set it with `rename` (on needs-input and idle, cleared on working).
- §9.6: placement: end-state configurability (naming scheme, prefix, session choice, windows vs panes, layout, rulesets) and the v1 placement.
- §13.1: third-party-reading sessions must not reach the user's tmux server.
- §20: this tool's constraints and open topics.

## Constraints (decided)

From architecture §20: `open` returns a stable window id; callers address windows by id, not by name. Outside tmux it targets the default server socket (configurable) and creates the session if missing.

## Open topics

From architecture §20:

- naming scheme
- status prefix mechanics
- end-state placement config

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What is the naming scheme for sessions and windows?
2. How does the status prefix work mechanically (base name vs displayed name, how `rename` applies and clears a prefix)?
3. What does the end-state placement config look like (session choice, new vs existing, windows vs panes, max panes, layout, rulesets)?
