# termctl: implementation plan

Status: planned, 2026-10-30. Implements [interface.md](interface.md) (normative) using the internal design in [design.md](design.md). Work is tracked as GitHub issues in this repository; each issue names its stage below. When an issue is picked up, its implementer expands it into bite-sized test-first steps (write the failing test, see it fail, implement, see it pass, commit) before writing code.

## Conventions for every stage

- Test-first. Every behavior in a task starts as a failing test.
- All subprocesses, `/proc` reads, `ps` calls and tty writes go through `src/termctl/gateway.py`; unit tests replace it with a fake. Time comes from an injectable clock.
- Integration tests run real tmux: each test pre-starts its own server with `tmux -f /dev/null -S <tmp path>` and passes `--socket <path>`; they never touch a user's server. They run in CI (tmux from apt, in termctl's own `tmux` job, which sets `TERMCTL_REQUIRE_TMUX` so a missing tmux fails them; the shared `verify` job cannot install packages, so it runs them only when the runner image ships tmux and skips them otherwise) and are not `live`. `make verify-floor` runs them with tmux 3.2 when it is installed locally.
- Every `--json` output gets a golden-file test validated against its schema in `schemas/` (with a `referencing` registry keyed by `$id`); every hook payload, log and audit record written in a test is validated too.
- Every spike finding listed in design.md "Live verification" that the implementation relies on becomes an integration regression test in the stage that relies on it.
- `make verify` passes before every commit. PR titles are Conventional Commit headers; no `!` before 1.0.
- Runtime dependencies: only `pyyaml` and `pydantic`. Test-only: `jsonschema`, `referencing`.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py` | 0 | argparse surface (global flags before or after the subcommand), dispatch, exit codes, check order |
| `argv.py` | 0 | the pre-scan of the command line: getopt-style option values, subparser and `--` positions, for the `--json`, `--version` and `--help` checks |
| `output.py` | 0 | writing results, errors (the `--json` error envelope on stdout), warnings and `--verbose` traces; escaping of human output |
| `errors.py` | 0 | error codes, the exit code table and the error envelope |
| `config.py` | 0 | YAML + `config.d` merge (maps deep, hook lists appended, other lists replaced), Pydantic models, `TERMCTL_*` scalars, load-time checks |
| `validate.py` | 0 | grammars: ids, names, marker names and texts, labels, sessions, placement names, env names, durations, actor, decision ids, client names, socket names and paths, cwd |
| `ids.py` | 0 | id generation, input normalization, prefix resolution (10/21, candidates) |
| `gateway.py` | 0 | the only boundary: subprocess (tmux, `ps`), `/proc`, `stat`/`lstat`/`mkdir`, tty writes, `pidfd` (config file reads in `config.py` are the documented exception, see design.md Internal architecture) |
| `procs.py` | 0, 4 | process start time and parent PID (Linux `/proc`, macOS `ps`), zombie detection, the `close --force` signal sequence |
| `jsonlog.py` | 0 | `log.jsonl` (level derived from result) and `audit.jsonl` |
| `lock.py` | 0 | per-server `flock` with 10 s wait, canonical-path hashing |
| `statedir.py` | 0 | state directory path and safe opening of its files (no symlinks, owner and mode checks) |
| `hooks.py` | 0 | hook execution (process group, timeout, stdin payload, env vars, decision id parsing and validation), `--hook` and `--override-escalation` handling |
| `tmux_bin.py` | 1 | `tmux_bin` resolution, the `tmux -V` version check |
| `tmux_backend.py` | 1 | tmux argument builder (all tmux argument rules), guarded-write wrapper, command chains |
| `tmux_socket.py` | 1 | server selection, the canonical socket path, socket safety checks |
| `panes.py` | 1 | the single `list-panes -a` read, field sanitizing, ownership check, option validation, deduplication, in-process filters |
| `backend.py` | 1 | the `Backend` protocol the commands use |
| `placement.py` | 2 | placement and rule resolution, inheritance from `default`, `not_supported` |
| `open_cmd.py` | 2 | the open order (steps 1–9), dry run |
| `display.py` | 3 | displayed-name computation, marker rules, rename eligibility, bell |
| `close_cmd.py` | 4 | the close order, audit |
| `focus_cmd.py` | 4 | the four focus steps, client selection, attach argv |

## Stage 0: Foundations

**Goal**: The CLI skeleton, config, grammars, ids, logging, locking and hooks, with no tmux behavior yet.
**Success Criteria**: `termctl --version` works; every command parses its full flag set (surface frozen per interface.md) and exits 2 on usage errors with the error envelope; config loads with drop-ins and `TERMCTL_*` scalars and fails fast with `config_invalid` naming the key or variable; `placements` (config-only) prints the effective placements and rule matches; hooks run per the design guide with veto, escalation and decision-id handling.
**Tests**:

- argparse: every command and flag of interface.md "Commands" and "Global flags"; global flags before and after the subcommand, never after `--`; `--verbose` with `--quiet` → 2; `--grace` without `--force` → 2.
- config: defaults equal `config.example.yaml`; deep map merge, hook lists appended, other lists replaced; unknown `TERMCTL_*` ignored; invalid scalar → `config_invalid` naming the variable; a rule naming an undefined placement and `async` on pre-hooks rejected at load; unsupported placement fields not rejected at load.
- grammars: every grammar in interface.md (positive and negative tables), including `#[`, control characters, marker text ending in `#`, socket `.`/`..`/`/`, decision ids, actor default fallback.
- ids: generation (base32), lower-casing, prefix rules (short → 2, ambiguous → 21 with candidates, duplicate full id → 21).
- `log.jsonl`/`audit.jsonl` records validate against their schemas; level derived from result.
- lock: second holder waits, times out at 10 s with exit 12 `locked`; released when the holder dies; never held during hooks.
- hooks: order, veto stops later hooks (11), escalation lets later hooks run (13), timeout counts as veto, process-group kill on timeout, payload on stdin, `TERMCTL_HOOK`/`TERMCTL_ID`/`TERMCTL_OBJECT_TYPE`, decision ids from stdout validated (bad ones dropped with a warning), `--hook` name and timeout rules, `--override-escalation` gate (TTY or `--actor human:*`) and exit-13-as-allow.
- `placements --label`: rule matching, `origin` values.

**Status**: In Progress

## Stage 1: tmux core and the read path (0.1)

**Goal**: termctl can find the right server safely, build tmux commands that cannot be injected, and read managed panes.
**Success Criteria**: `show`, `exists` and `list` work against real tmux on 3.2 and current versions, including no-server, missing-parent and dead-pane cases; panes with forged or inherited options are ignored; every tmux argument rule is covered by an integration test.
**Tests**:

- server selection order (`--socket`, `TERMCTL_SOCKET`, config, `$TMUX`, default) with `TMUX_TMPDIR`; always `-S`; canonical `socket_path` (deepest existing ancestor); 100-byte limit.
- socket safety: default-directory form (owner, mode `& 0o027`, symlink), other paths (owner uid or root, no group/other write, no sticky exception), existing socket owned by the uid; missing parent or socket → no server (10 / empty list / `exists` output).
- socket path ancestry: every ancestor from `/` to the parent owned by the uid or root, group/other-writable ancestors only when sticky (e.g. `/tmp`); an ancestor owned by another user or writable without sticky → 12 `server_unavailable`, tmux never invoked.
- tmux version parsing (`3.2a`, `3.5a`, `next-3.6`, unparsable) and the 3.2 floor (`tmux_too_old`); `tmux_bin` resolved once to an absolute path; `tmux_missing`.
- argument builder regression tests from spikes E1, E3, E4, C9, F5: trailing `;` in every position (no injected command runs), `--` before the first positional, `#` doubling in `-n`/`-c`/`-s`/`rename-window` (including `#(` never executing), quoting inside the guarded-write string.
- `panes.py`: tab-separated read with sentinel, sanitized free-text fields, `@termctl_pane` ownership (window/session/global values rejected; spike F4), every option grammar, duplicate rows from linked windows (spike D6), unknown `@termctl_v` ignored with a warning, in-process `--label`/`--session` filters, list order (created, then id).
- human output escaping: C0, DEL and C1 characters in tmux-, hook- and pane-option-derived strings (names, labels, marker text, `hook_stderr`, tmux error text) are escaped as `\xNN` on stdout and stderr in human mode; `--json` output unchanged.
- untrusted pane fields: the pane schema marks `name`, `labels`, `marker`, `marker_text`, `placement`, `pid` as `x-untrusted` (schema test); a forged but grammar-valid option value is reported as read.
- `show`/`exists`/`list` outputs: golden files for running, dead (with and without exit status; `exit_signal` null on 3.2), and the `exists` exit-10 output.

**Status**: In Progress

## Stage 2: open (0.2)

**Goal**: The full open order: placement, session creation, the placeholder and respawn, the no-argv shell path, environment scoping, PID recording, hooks and focus-on-open.
**Success Criteria**: `open` creates a managed pane in the configured placement on a running or absent server, under a user config with `destroy-unattached on` and `exit-unattached on`, and the pane carries every option before the real command can exit; `--env` values reach only that pane; a cron-like caller's credentials never reach tmux; dry runs change nothing.
**Tests**:

- check order: usage (including the cwd) → resolve (server, `tmux_bin` with `tmux_missing`, placement, id, `command_not_found` on the filtered or `--env` PATH, size limit 12288 bytes on the worst case) → tmux checks and id-collision check → pre-open hooks → session → pane → record → focus → post-open.
- placement: rules (first match, empty match), `--placement`, `--session`/`--target` overrides, inheritance from `default`, `not_supported` for `target: pane` and related fields.
- session creation: exact `=S:` targets; duplicate-session race (two concurrent opens, one server, both panes created); `destroy-unattached off` chained (spike C2); `exit-unattached off` only when the re-probe found no server (spike F2); `has-session` confirmation; the vanished-session retry, never exit 10; re-run socket checks and `mkdir`/`EEXIST`/`lstat` before starting a server.
- placeholder + respawn (spike C1, E5): options set before a fast-exiting command (`true`, `false`); keep `never|failed|always`; no leftover placeholder; `state: gone` output; `-d` always; PID recorded only when not dead and parented by the server (spike E6).
- no-argv path: login shell (spike E2); `-e` via `new-window`, or `new-session` + `set-environment` replacing the session entry with the filtered value, or `-r` when the filtered environment lacks the variable (spike G3c; later windows get the filtered value or none, never the server's global value, even for a variable outside `update-environment`).
- environment: every tmux invocation runs with the filtered environment (spike C7, D3); `--env` never in the session environment (spike G1–G3); `--env PATH=X` via `/usr/bin/env -- PATH=X` (spike G4); `--env PATH` without argv → 2; repeated names → 2; values redacted under `--verbose`.
- failure handling: `tag_failed` with the guarded cleanup kill; no post-open hooks; log `result: error`.
- hooks: pre-open payload fields (env names only), veto/escalate/override, decision ids in output, log and audit (override runs).
- `--dry-run`: read-only tmux queries only, tmux checks, `%NEW`/`@NEW` tokens, no hooks, nothing created.

Interim state: the CLI `open` runs steps 1 to 3, 5, 6 and 7 (no hooks yet; until #17 it refuses, exit 1 `internal`, before anything is created whenever a `pre-open` or `post-open` hook is configured or given with `--hook`), then reads the pane back for its output. Its only `log.jsonl` record is the `result: error` record of `tag_failed` (#17 adds the others); output has `focused: false` until focus-on-open lands (#24) and only the `--decision-id` values (#17). `open --dry-run` still refuses (exit 1, `internal`) until #18.

**Status**: Not Started

## Stage 3: rename, markers and labels (0.3)

**Goal**: Guarded mutations of managed panes: rename, mark, unmark and labels, with display rules and dry runs.
**Success Criteria**: supervise-style polling (`mark`/`unmark` every second on 20 panes) writes no log records and takes no lock contention failures when nothing changes; a stale pane id after a server restart is never acted on; windows the user split or joined are never renamed.
**Tests**:

- guarded writes: a restarted server reusing `%N` with a different pane → exit 10, nothing changed (spike F5); pane vanishing mid-command → 10.
- display: marker text + name; `marker_display: option`; rename eligibility (same window, single pane; `display_updated`); no-op rules for `mark` and `unmark` including the displayed-name comparison; marker removed from config later; `#` doubling round trip (spike F names).
- bell: written only for running panes, `O_NOCTTY`, char device only, failure warns.
- labels: multiset semantics, 32 limit after id resolution, zero pairs → 2, `display_updated: false`.
- dry runs for `rename`, `mark`, `unmark`, `label add/remove`: computed vs current display name per interface "Dry runs".
- dead panes accept all of these.

**Status**: Not Started

## Stage 4: close and focus (0.4)

**Goal**: The close order with the human-only force path and audited signals, and focus with explicit client selection and attach.
**Success Criteria**: `close` never kills a running pane without a human `--force`, never signals a process other than the recorded one, and kills only the managed pane in a split window; `focus` never moves a client on another session by default, attaches the current terminal before switching any other client, and gives `--json` callers an attach argv that works when exec'd.
**Tests**:

- close: running without `--force` → 20 `running`; `--force` gate (TTY or `--actor human:*`); full id required with `--force`; pre-close hooks then re-check; `state_changed` (12) when the state changed; gone at re-check → 0 with post-close hooks; `kill-pane` only (spike D5); kernel SIGHUP then grace, SIGTERM, 5 s, SIGKILL, 1 s (spike F3); PID plus start time check before every signal, `pid_mismatch`; `@termctl_pid` not equal to the same row's `#{pane_pid}` at step 1 or at step-3 re-resolution → `pid_mismatch: true`, no signal ever sent; pane gone at re-check signals only a PID that passed the step-1 check; zombie counts as exited; survives SIGKILL → `exited: false`, exit 0; audit records for forced and override runs; `--dry-run` (`signals_possible`).
- focus: step 1 only for clients on `$TMUX_PANE`'s session (validated `%N`); step 2 when stdin and stderr are terminals (exec in human mode; `--json` returns the 7-element attach argv with `-E`); step 3 most recent client preferring the pane's session, always `-c`; step 4 `no_client`; `--client` skips steps 1–2; `-E` keeps the session environment unchanged (spike F1); `=S:@W.%P` targets (spike D2, F7); log written before exec; `--dry-run`.

**Status**: Not Started

## Stage 5: Platform checks and 1.0 readiness

**Goal**: Confirm the floor and platforms and finish the user-facing docs.
**Success Criteria**: the full integration suite passes on tmux 3.2 (`make verify-floor`) and the CI version; a manual macOS checklist (open, mark, focus attach, close `--force`, socket safety under `/private/tmp`) is recorded in design.md; README matches the implemented behavior; agent-spawn's flows (open with labels, store id and `socket_path`, mark/unmark, focus `--json` attach, close) pass a scripted end-to-end run against real tmux.
**Tests**:

- `make verify-floor` on tmux 3.2a.
- macOS checklist (manual; start-time resolution, `ps` parsing, `/private/tmp` canonicalization).
- agent-spawn flow script (in this repo's integration tests, using only termctl's CLI).

**Status**: Not Started

## Version roadmap

- **0.1** (stage 1): read path and safety foundations (`show`, `exists`, `list`, `placements`).
- **0.2** (stage 2): `open`.
- **0.3** (stage 3): `rename`, `mark`, `unmark`, labels.
- **0.4** (stage 4): `close`, `focus`. Feature-complete for agent-spawn's v1 needs.
- **1.0**: stage 5 done; agent-spawn has used termctl for the v1 ticket slice without interface changes; the interface is declared stable (a deliberate breaking-change commit per architecture §18.3).
- **1.x** (additive): `target: pane` placements (`max_panes`, `layout`, `group_by`), which the config schema already reserves; markers on panes (pane titles) when panes share a window.
- **2.0**: reserved for a breaking change, such as a second terminal backend that requires changing ids or outputs.
