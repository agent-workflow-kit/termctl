# termctl: design

Status: designed (interface 0.1.0-design), 2026-10-30. The interface, including every normative rule, is in [interface.md](interface.md); this document records the decisions, the internal architecture and the live verification behind them, and links to the interface instead of restating its rules.

## Role and layer

Terminal placement, tmux backend. Layer 1 (primitive), fully standalone (architecture §2, C2). termctl knows nothing about Claude or its callers; agent-spawn is its only awkit consumer (interactive window placement and the status marker, architecture §9).

## Constraints (from architecture §20)

- `open` returns a stable id and callers address objects by id, not by name: [Identifiers](interface.md#identifiers).
- Outside tmux it targets the default server socket (configurable) and creates the session if missing: [Server selection](interface.md#server-selection), [Open order](interface.md#open-order).

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | termctl owns placement: named placements plus label-matched rules in its config; explicit flags override ([Placement](interface.md#placement)). | Sessions, windows, panes and layouts are terminal concepts; keeping them out of agent-spawn keeps the terminal boundary clean for a later backend (C5) and gives every caller the same placement. Label matching in user config is allowed (design guide §9). |
| D2 | v1 implements only window placement (a window per pane) with exact-match rules; the richer placement fields are in the schema but rejected as not supported ([Placement](interface.md#placement)). | Architecture §19.2 excludes richer placement from v1; designing the schema now avoids a breaking change later. |
| D3 | Stateless: tmux pane options are the only store ([Pane options](interface.md#pane-options)); no `state.db`, no `reconcile`. | tmux is authoritative for windows; a mirror could only drift. History comes from `log.jsonl`. |
| D4 | An id names a pane and is stored in a pane option ([Identifiers](interface.md#identifiers)). | Survives the later move to several panes per window without changing ids; tmux's own `@N`/`%N` ids restart with the server (spike A1). |
| D5 | Friendly display names; the id never appears on screen; duplicate names allowed ([Names](interface.md#names)). | User preference; callers address by id. |
| D6 | Named markers with their text in config, displayed by renaming the window by default or only through an option ([Markers](interface.md#markers)). | Renaming is the only marker visible without editing tmux config (spike B6); the option form serves users who prefer a pure name. Glyphs stay in termctl; agent-spawn only says `attention`. An earlier `both` value was dropped (D25). |
| D7 | `close` refuses a running pane with termctl's own reason `running`; a human `--force` overrides it, kills the managed pane and then signals only the exact recorded process, verified by start time; `--force` is gated like `--override-escalation` (D23) ([`close`](interface.md#close), [Errors](interface.md#errors), [Residual risks](interface.md#residual-risks)). | Windows usually host running agents; tmux itself sends no signal on close (spike B3, F3); the architecture allows signals only to exact verified PIDs (§3.4), so never a process group, and the process's children get only the kernel's SIGHUP. The reason is termctl's own `running`, not the shared `live`: the design guide's `live` rule (§4.4, §12) protects sessions, agents and worktrees and is never overridden, while a human must be able to force-close a hung terminal pane. Keeping the reasons distinct leaves that shared rule untouched. The gate is advisory, as for D23. The recorded PID is trusted only when it matches the pane's own `#{pane_pid}`, since anything with socket access can write `@termctl_pid` ([`close`](interface.md#close), [Liveness](interface.md#liveness)). |
| D8 | argv is always executed directly, never through a shell; there is no shell-string flag ([Command](interface.md#command)). | tmux runs a single argument through `sh -c` (spike A4); one rule, no quoting surprises. |
| D9 | `--cwd` is validated and defaults to the caller's cwd ([`open`](interface.md#open)). | tmux silently falls back to `$HOME` for a missing directory (spike A4); the caller's cwd is what a human expects. |
| D10 | `--env` is for non-secret values only and reaches only the opened pane, never a session's stored environment; every tmux invocation runs with an allowlisted environment; termctl's own attach and client switch keep the client's environment out of the session ([Environment](interface.md#environment), [Open order](interface.md#open-order), [Residual risks](interface.md#residual-risks)). | `-e` values are visible in process listings (spike A5). tmux takes the server environment, a new session's imported variables and the pane's `PATH` from the invoking process (spike C7, D3), so cron's credentials or an SSH agent socket would otherwise reach every later window; the remedy for a missing variable is the `server_env_allow` config, not a wider default. `new-session -e` stores the variable in the session environment, where every later window inherits it, while `new-window -e` and `respawn-pane -e` are pane-only, and removing a variable with `set-environment -u` right after `new-session -e` leaves it only in the first pane (spike G1–G3; `-r` instead leaves a removal marker that strips the variable from later windows). A client that attaches or switches without `-E` imports its `update-environment` variables into the session (spike F1); a user's own plain `tmux attach` stays a residual risk. |
| D11 | `--keep-on-exit` lets a caller keep a pane after its command exits; by default the pane closes ([Command exit](interface.md#command-exit)). | Windows must close when an agent finishes; callers that want crash output opt in. |
| D12 | termctl resolves the server itself and always passes `-S` with an absolute socket path, never `-L`, also for a socket given by name; outputs carry `socket_path` ([Server selection](interface.md#server-selection)). | Works for one-server and several-server users; stored handles stay exact across cron and interactive contexts. Resolving a socket name before environment filtering means the filtered environment can never change which server is reached, and the reported `socket_path` is exactly the one used. |
| D13 | `focus` chooses the client explicitly instead of relying on tmux's default (user decision Q17, 2026-10-30); `--client` names one; an attach uses `-E`, and under `--json` termctl returns the attach argv instead of replacing its own process ([`focus`](interface.md#focus)). | tmux's default client choice picks the most recently active client of the pane's session, or of another session when none is attached (spike D4); `switch-client` needs a client and `attach` needs a tty and refuses nesting (spike B1, B2); `attach -t '=S:@W.%P'` selects window and pane in one step (spike D2). Returning the argv lets a caller that owns the terminal attach with its own environment. The terminal test leaves stdout out so `--json` (stdout to a pipe) stays usable from a terminal; `-E` keeps the attaching client's environment out of the session (D10, spike F1). |
| D14 | Hooks `pre-open`, `post-open`, `pre-close`, `post-close`; every mutating invocation (not dry runs or no-ops) past the usage check is logged, failures included; `close --force` on a running pane and runs with `--override-escalation` are audited ([Hooks](interface.md#hooks), [Audit and log](interface.md#audit-and-log)). | Mark changes are frequent and already covered by agent-spawn's state hooks; `close --force` is the only destructive action, and an override run is the only way past a policy hook; both are human decisions worth a record. Logging failures too makes the log a complete history for a stateless tool (D3). |
| D15 | Names and marker texts are validated and rejected, never cleaned; every tmux argument goes through one builder ([Names](interface.md#names), [tmux argument rules](interface.md#tmux-argument-rules)). | tmux format-expands `-n`, `-c`, `-s` and `rename-window` values and runs `#(...)` in them as shell commands (spike E4); a trailing `;` in any argument element splits the invocation into a new tmux command (spike E1); a leading `-` is parsed as a flag unless `--` precedes the first positional (spike E3); doubling does not neutralize `#[`, which the status line applies as a style (spike C9). One builder makes these rules impossible to skip; silently changing input surprises callers. |
| D16 | tmux binding A (hard) behind an internal `Backend` interface; tmux 3.2 floor ([Platform notes](interface.md#platform-notes)). | One backend now, a clean seam for another later (C5); 3.2 is the oldest version in current Ubuntu LTS releases and was verified directly. |
| D17 | Dependencies: `pyyaml`, `pydantic` only; no `libtmux`. | The design guide's standard set; `libtmux` would hide exactly the details the spikes showed matter (argv exec, escaping, exact targets). |
| D18 | Real tmux runs in CI on a private socket (not `live`). | Free and deterministic; the spike findings become regression tests. |
| D19 | `open` with argv uses a placeholder pane, sets the options, then respawns the real command; `open` without argv creates the pane with no command and sets the options afterwards ([Open order](interface.md#open-order)). | Setting options after a fast-exiting command loses the race (spike B4); one code path for every keep mode. A respawn cannot reproduce tmux's login shell (spike E2), and a shell does not exit at once, so the no-argv case needs no placeholder. |
| D20 | Read-modify-write steps take a per-server `flock`, never held while hooks run ([Audit and log](interface.md#audit-and-log)). This deviates from design guide §4.3, which prescribes a lease in `state.db`. | termctl is stateless (D3) and has no `state.db`; tmux serializes single commands but not termctl's read-then-write sequences; the kernel releases a `flock` when its holder dies, so no lease expiry or stale-lease recovery is needed. |
| D21 | Liveness comes from tmux's dead-pane state; the recorded PID is used only for signaling; pane objects carry one `state` field ([Liveness](interface.md#liveness)). | tmux knows whether the pane's process ended, including for panes whose PID was never recorded; a PID check alone can be fooled by PID reuse, and a recorded PID that does not match the pane's `#{pane_pid}` is never used ([`close`](interface.md#close)). One string field replaces two booleans that could contradict each other. |
| D22 | `close` kills only the managed pane, never the window; a single-pane window closes with it ([`close`](interface.md#close)). | `kill-window` would also kill panes a user split into the window, which termctl does not own (spike D5). |
| D23 | `open` and `close` accept `--override-escalation` from a human (the same advisory gate as `--force`): an escalation from a pre-hook is treated as allow, a veto still vetoes, every pre-hook is told it is an override run, and the actor and decision ids are passed to hooks and recorded ([Hooks](interface.md#hooks), [Audit and log](interface.md#audit-and-log)). This deliberately deviates from design guide §4.4, whose `confirm` overrides only the escalating hook's verdict and re-runs the others. | Gives a human a recorded way past an escalating policy hook without weakening vetoes. termctl is stateless (D3) and cannot know which hook escalated on an earlier run; telling every hook hands that choice to the hooks, so a policy can veto override runs. Pre-open object ids are per attempt, so gates correlate override runs by decision ids and actor, not by id. The gate is advisory: any pane has a TTY and the actor is self-declared ([Residual risks](interface.md#residual-risks)). |
| D24 | Every argument termctl passes to tmux goes through one builder that applies the [tmux argument rules](interface.md#tmux-argument-rules). | See D15; the rules apply to every element, so they live in one place in `tmux_backend.py`. |
| D25 | `marker_display` has no `both` value ([Markers](interface.md#markers)). | The marker options are always set, so `both` behaved exactly like the rename form; no behavior is lost. |
| D26 | termctl checks the socket's directory and the socket itself before reaching a server, and `open` repeats the checks and the server probe right before it starts one; a failed check never reaches the server ([Server selection](interface.md#server-selection), [Open order](interface.md#open-order), [Residual risks](interface.md#residual-risks)). | `-S` performs none of the checks `-L` does (spike F6), and termctl always uses `-S` (D12); a socket in a directory others can write could be replaced by another user's server. `-S` with a missing parent exits 0 without a server (spike F6), and `kill-server` leaves the socket file behind (spike F), so the socket's presence alone never proves a server. Hooks run between the first check and the server start, so `open` checks again. A hostile server could lie about its own PID, so the directory checks, not a server-reported uid, are the defense; like OpenSSH's path check, they cover every ancestor directory of the socket, since a writable ancestor allows replacing the whole path ([Server selection](interface.md#server-selection)). |
| D27 | A pane is managed only when its own pane-level ownership option names that pane; termctl writes every option at pane level, and every value it reads is validated, a failing pane being ignored with a warning ([Managed panes](interface.md#managed-panes), [Pane options](interface.md#pane-options)). | `#{@option}` formats and `-f` filters inherit window, session and global values, so a value set at a wider scope would make every pane look managed (spike F4); a pane's own id cannot be inherited correctly by another pane. Anything with socket access can write options, so read values are untrusted input and deliberate forgery is a [residual risk](interface.md#residual-risks). |
| D28 | Every mutating tmux operation on a managed pane, including the cleanup kill after a failed `open`, runs inside a tmux-side guard that re-checks ownership in the same tmux command ([tmux argument rules](interface.md#tmux-argument-rules), [Open order](interface.md#open-order)). | `%N` ids restart with the server (spike A1), so after hooks or any delay a stored `%N` may name another pane; the guard makes the check and the action one tmux command. A missing target evaluates the condition as false instead of falling back to the active pane, and inner commands without `-t` would run against the default target (spike F5). |
| D29 | `rename` and `mark` rename the window only while termctl still owns it alone; otherwise they update the options only and say so in the output ([`rename`](interface.md#rename), [`mark`](interface.md#mark)). | A window a user split or a pane moved elsewhere (spike D5) belongs partly to someone else; renaming it would overwrite a name termctl does not own. |
| D30 | Only a server `open` starts itself gets `exit-unattached` turned off; an existing server's settings are never changed ([Open order](interface.md#open-order)). | Under a user's `exit-unattached on` a server started detached exits at once (spike F2); the prefixed command runs after the config file is loaded, so it wins. Changing a running server would override a user's choice. |
| D31 | Every command checks in one fixed order, and `open` resolves id collisions before its pre-open hooks, so hooks run once ([Check order](interface.md#check-order), [Open order](interface.md#open-order), [Exit codes](interface.md#exit-codes)). | One order gives predictable exit codes; checking collisions before hooks keeps the id a hook sees the id that is used. |
| D32 | Unsupported placement fields are rejected only when a placement using them is resolved; loading config for other commands never fails on them ([Placement](interface.md#placement), [Config](interface.md#config)). | A config written for a later version must not stop `mark` or `close` from working on panes that are already running. |
| D33 | `--env PATH=X` reaches the program through `/usr/bin/env` in the pane's argv, never through tmux or the server's environment ([Command](interface.md#command), [Environment](interface.md#environment)). | tmux ignores `-e PATH` and gives the pane the invoking process's `PATH` (spike C7); setting it through `env` reaches the program without changing the server's environment. GNU `env` takes assignments only before the command, after its own `--` (spike G4). |
| D34 | `close` does nothing and exits 12 `state_changed` when the pane's state at the final re-check differs from its first check; the caller may retry, which runs fresh hooks ([`close`](interface.md#close), [Errors](interface.md#errors)). | Hooks and a human's `--force` decide on the state they saw; acting on a different state (a pane that died, or one respawned into a running command) would carry out a decision nobody made. Retrying is cheap and re-runs every check. |
| D35 | `open` never exits 10: a session that vanishes mid-`open` is retried once through the new-session path, then reported as a server problem ([Open order](interface.md#open-order), [Exit codes](interface.md#exit-codes)). | Exit 10 reports a missing id, server or client that the caller depends on; `open` creates the server and session it needs, so a session vanishing mid-`open` is a race it can recover from, not a missing object. |

## Open topics resolved

| Topic (architecture §20) | Resolution |
|---|---|
| Naming scheme | D5, D15; [Names](interface.md#names), [Placement](interface.md#placement). |
| Status prefix mechanics | D6: named markers, base name kept in `@termctl_name`, displayed name recomputed. |
| End-state placement config | D1, D2. |

## Internal architecture

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, global flags, output and error rendering, exit codes |
| `errors.py` | error codes, their exit codes and the `{"error": {...}}` envelope; separate from `cli.py` so every module can raise them without importing the CLI |
| `config.py` | Pydantic models, file and drop-in merge, `TERMCTL_*` scalars, validation |
| `ids.py` | id generation and prefix resolution |
| `validate.py` | input grammars: name, label, marker, session, placement, env-name, duration, actor (and its default), decision-id, client, socket and cwd |
| `placement.py` | placement and rule resolution |
| `backend.py` | the `Backend` protocol (open, close, list, rename, mark, focus, labels) |
| `tmux_backend.py` | the tmux argument builder ([tmux argument rules](interface.md#tmux-argument-rules)), guarded writes (`if-shell` wrapping, D28), format strings, command chains, version check, server env filtering, socket path resolution and socket directory checks (D26) |
| `panes.py` | the pane reader: parses `list-panes -a -F` records, applies the ownership check and validates every `@termctl_*` value (D27) |
| `procs.py` | process start time and parent check for the recorded PID, the `close --force` signal sequence |
| `gateway.py` | the only module that runs subprocesses or touches the filesystem outside config and logs (tmux, `ps`, `/proc`, socket `stat`, the cwd check, stdin TTY test and OS user name, BEL write); also holds the injectable clock |
| `hooks.py` | hook execution per the design guide |
| `jsonlog.py` | `log.jsonl` and `audit.jsonl` |
| `lock.py` | per-server `flock` (D20) |

Data flow for `open`: `cli` → `config` + `validate` → `placement` → `tmux_backend` + `panes` (socket checks, version check, id-collision check) → `hooks` (pre) → `tmux_backend` (session, placeholder pane, options, respawn) → `procs` (record PID) → `hooks` (post) → `jsonlog`. All tmux reads use one `list-panes -a -F` call with tab-separated fields, free-text fields sanitized in the format string and an end-of-record sentinel (spike A8). Targets are always exact ids (`=session:`, `@N`, `%N`), never names or indexes (spike A6, B7).

Error handling: usage and config problems are detected before any tmux call; tmux failures map to exit 12 codes; a pane that vanished mid-command or a guard non-match maps to exit 10 (D28), except in `open` (D35) and in `close`, where a pane gone at the guarded kill counts as closed (exit 0, [`close`](interface.md#close)); a partially created pane is killed under the ownership guard (D28, [Open order](interface.md#open-order)).

## Implementation notes

- Signaling (`procs.py`): on Linux termctl opens a `pidfd_open` handle on the recorded PID before `kill-pane`, verifies the start time, and signals through `pidfd_send_signal`, so a PID reused after the handle was opened is never signaled. A zombie counts as exited.
- BEL (`bell` marker, `gateway.py`): the pane's tty is opened with `O_WRONLY|O_NOCTTY|O_NONBLOCK` and must be a character device; anything else is not written to.

## Live verification

Spikes ran on 2026-10-29/30 against tmux 3.5a and 3.2a on Linux, each on its own `-L` socket with `-f /dev/null`, never touching the user's server.

| Finding | Version notes | Relied on by |
|---|---|---|
| `@N`/`%N`/`$N` ids are never reused within a server's life and restart at 0 after a server restart; `start_time` has 1-second resolution. | both | D3, D4 |
| User options survive rename, move and link, are never format-expanded on output, accept any printable value up to ~16 KB per command; `set -o` is atomic set-if-unset. | both | D3, D4 |
| Pane options show in `list-windows` only for the active pane; `list-panes -a` reads them reliably; `-f` filters work from 3.2. | `set -p` since 3.0, `-f` since 3.2 | D4 |
| One argv element runs through `default-shell -c`; two or more are executed directly. | both | D8 |
| A missing `-c` directory silently falls back to `$HOME`; `-c` is format-expanded. | both | D9 |
| Windows get the server's global environment, not the caller's; `-e` works on `new-window` (3.0+) and `new-session` (3.2+); `-e` values are visible in `ps`; `TMUX`/`TMUX_PANE` are always set. | both | D10 |
| A cron-like start (no tty, minimal env) auto-starts a server; the loser of a session-creation race gets `duplicate session`. | both | Open order step 5 |
| `-t foo` prefix-matches `foobar`; `=foo` is exact; `.` and `:` in session names become `_`. | both | D15, session grammar |
| `-n`/`rename-window` names are format-expanded when set; `##` round-trips; `#[style]` applies on the status line unless escaped. Spike A saw no `#()` execution; spike E4 corrected this: `#(...)` in `-n`, `-c`, `-s` and `rename-window` does run a shell command in the server. | both | D15, D24 |
| `allow-rename` is off by default; `-n` and `rename-window` turn `automatic-rename` off for the window. | both | D5, D6 |
| `switch-client` without a client fails ("no current client"); the default client choice is session-first; `-c` targets exactly. | 3.5a | D13 |
| `attach` needs a tty and refuses to nest inside a pane. | 3.5a | D13 |
| `kill-window` sends no signal; the kernel's SIGHUP on pty close is the only one; HUP-ignoring processes survive. | 3.5a | D7 |
| Setting `remain-on-exit` in a second call loses the race against a fast-exiting command. | 3.5a | D19 |
| A user's `destroy-unattached on` removes detached sessions at once. | 3.5a | Open order step 5 |
| The socket gives full control to any process that reaches it; socket paths over ~107 bytes fail. | both | Residual risks, socket limit |
| One `list-panes -a -F` over 200 windows takes ~10 ms; each tmux call costs ~7–11 ms. | 3.5a | Data flow |

Each finding becomes a regression test in the real-tmux integration suite (plan stage 2).

### Spike C: final command sequences

Run on 2026-10-30 against tmux 3.5a and 3.2a; identical results on both.

| Finding | Relied on by |
|---|---|
| Historical: the spike used the placeholder `/usr/bin/env -- cat`; the current placeholder is `sleep 30` ([Open order](interface.md#open-order)). Placeholder pane, then one chained invocation setting pane and window options and `respawn-pane -k -c DIR -e VAR=v`: options are set before the real command can exit; with `remain-on-exit off` a fast-exiting command's window closes, with `on` it stays dead with its status and options readable; `pane_pid` is the real command (env execs); `respawn-pane -k` does not close the window; no placeholder is left over; `-c` and `-e` work on 3.2. | D19, Open order step 6 |
| With a global `destroy-unattached on`, a new detached session is destroyed before a separate invocation can set the option; chaining `set -t '=S:' destroy-unattached off` into the `new-session` invocation keeps it. | Open order step 5 |
| A failing command in a `\;` chain stops the chain with exit 1 and one stderr message; earlier commands are not rolled back and their output (including `-P`) is still printed. | Open order failure handling |
| A pane option is visible to a user's `window-status-format` and through a window target in `display`. | Status format |
| `list-panes -a -f` works on pane options with `==` and `m`; unescaped `,` or `}` in a filter value makes it match every pane. termctl therefore never uses tmux `-f` filters; it filters its own `list-panes -a` read. | Lookup |
| `kill-window`, `rename-window`, `select-window`, `respawn-pane` accept `%N`; `switch-client -c C -t %N` switches session and window and selects the pane. | focus, close |
| The caller's `PATH` reaches the pane (overriding even `-e PATH=`), and `new-session` imports the caller's `update-environment` variables (`DISPLAY`, `SSH_*`); a variable absent from the caller's environment never reaches the pane. | D10 |
| BEL written to the pane's tty sets the bell flag on a window that is not current, never on the current one. | Markers (`bell`) |
| Doubling each `#` run round-trips except before `[`: `#[` is kept as written and later applied as a style. | D15 |
| `-t '=S'` without the trailing colon fails for `set` and silently resolves to nothing for `display`; `=S:` works everywhere. | Exact targets |

### Spike D: exit modes, attach, environment, clients, pane layout

Run on 2026-10-30 against tmux 3.5a and 3.2a; identical results on both except where noted.

| Finding | Version notes | Relied on by |
|---|---|---|
| `remain-on-exit failed` is accepted as a window and a pane option: a zero exit closes the window, a non-zero exit keeps the pane dead with its status, a signal death keeps it with an empty `#{pane_dead_status}`. `#{pane_dead_signal}` exists only on 3.5. On 3.2 a pane can briefly (about 50 ms) show dead with an empty status even for a zero exit. | `pane_dead_signal` 3.3+; the brief empty status only on 3.2 | D11, [Command exit](interface.md#command-exit), pane `exit_status`/`exit_signal` |
| `attach -t '=S:@W.%P'` (and `attach -t %P`) attaches with the window and pane selected; `attach -t =S` followed only by `select-pane` leaves the client on the old window. Attaching changes the session's current window for its other clients. `attach` exits 0 on detach or when the session ends, 1 when the server is killed. | both | D13, [`focus`](interface.md#focus) |
| A session created from a caller lacking `SSH_AUTH_SOCK` records its removal; that session's panes never see it, even from later callers that have it; `-e` overrides it. A window in a session created with the variable sees it. The pane always gets the caller's `PATH`. | both | D10, [Environment](interface.md#environment) |
| From inside a pane, `display` and `switch-client` without `-c` pick the most recently active client of the pane's session, or a client of another session when none is attached; `display` without `-t` reports the client's pane, not `$TMUX_PANE`; `list-clients -f` does not exist on 3.2. | `list-clients -f` 3.3+ | D13 |
| `kill-window -t %N` kills every pane of the window; `kill-pane -t %N` kills only that pane; `join-pane` moves the pane id to another window and destroys the original window id; `#{window_panes}` works in `list-panes -a -F`. | both | D22, [`close`](interface.md#close) |
| A window linked into two sessions gives one `list-panes -a` row per session link, with the same window and pane ids; `display -t %N` resolves to the link target's session. | both | [`list`](interface.md#list) (one row per pane id) |

### Spike E: tmux argument parsing

Run on 2026-10-30 against tmux 3.5a and 3.2a from a Python argv list; identical results on both except where noted.

| Finding | Version notes | Relied on by |
|---|---|---|
| Any argument element ending in an unescaped `;` (including after `--` and in `-n`, `-c`, `-e` and option values) loses that `;` and ends the tmux command; the following elements run as a new tmux command. Replacing the final `;` with `\;` round-trips every tested string; `;` elsewhere is literal. | both | D15, D24, [tmux argument rules](interface.md#tmux-argument-rules) |
| `respawn-pane` without a command re-runs the pane's original command; with an empty command the pane exits at once; no argv reproduces tmux's login shell, which a pane created without a command gets. | both | D19, [Open order](interface.md#open-order) |
| A leading `-` in a positional argument is parsed as a flag unless `--` comes immediately before the first positional; `--` between two positionals is an error; option arguments such as `-n -x` are taken verbatim. | both | D24, [tmux argument rules](interface.md#tmux-argument-rules) |
| `-n`, `-c`, `-s` and `rename-window` values are format-expanded and `#(...)` in them runs a shell command in the server (this corrects spike A); `-e` values, argv after `--` and option values are not expanded; `#[` runs are left as written. Doubling every `#` run not followed by `[` round-trips. A `-c` that expands to a missing directory silently falls back to `$HOME`. | both | D9, D15, D24, [Names](interface.md#names) |
| A placeholder replaced by `respawn-pane -k` leaves no dead pane, under `remain-on-exit failed` and `on`; exit status and signal behave as in spike D. | `pane_dead_signal` 3.3+ | D19, [Command exit](interface.md#command-exit) |
| `display -p -t %N '#{pane_pid} #{pane_dead} #{pid}'` chained after `respawn-pane` reports the new PID, not dead; the pane process's parent is the server (`#{pid}`). `display -t` with a missing pane exits 0 with empty fields, so existence is tested with `list-panes -t %N`. | both | [Open order](interface.md#open-order) step 7, [Liveness](interface.md#liveness) |

### Spike F: client environment, server options, ownership, guards, socket checks

Run on 2026-10-30 against tmux 3.5a and 3.2a, each on a private `-S` socket with `-f /dev/null`; identical results on both except where noted.

| Finding | Version notes | Relied on by |
|---|---|---|
| A session created without `SSH_AUTH_SOCK` gets it once a client that has it runs `attach` or `switch-client` without `-E`, and later panes see it; with `-E` the session environment is unchanged. Both commands accept `-E`. | both | D10, D13, [Environment](interface.md#environment) |
| `exit-unattached` is a server option whatever scope `set` names. With it on in the config, `new-session -d` leaves no server (exit 0); a chained `set exit-unattached off` or a prefixed `set -s exit-unattached off \;` keeps it, because the config is loaded before the client's commands run. | both | D30, [Open order](interface.md#open-order) step 5 |
| `kill-pane` and `kill-window` send no signal themselves; the process sees the kernel's SIGHUP on pty hangup, then SIGCONT. | both | D7, D22 |
| `#{@opt}` formats and `-f` filters see a value inherited from the window, session or global options in every pane that lacks its own; `show -pqv` reads only the pane's own value; a `@termctl_pane` equal to the pane's own id matches only that pane. | `show -p -v` of an inherited-only value: 3.5 exits 1, 3.2 exits 0 empty | D27, [Managed panes](interface.md#managed-panes) |
| `if-shell -F -t %N` runs its inner command only when the condition holds; a missing target exits 0 and evaluates the condition with an empty context (false), never against the active pane; a failing inner command exits 1 with its error. Inner commands need an explicit `-t`. The guard works with `kill-pane`, `rename-window` and `set -p`. | both | D28, [tmux argument rules](interface.md#tmux-argument-rules) |
| Single-quoting each value of the inner command (`'` → `'\''`) round-trips `;`, `#`, quotes, backslashes, `$`, `~`, braces, newlines and leading `-`; `rename-window` and `new-window -n` still format-expand the name, so the `#` doubling of spike E applies inside the guard too. | both | D15, D24, D28 |
| `-S` performs no directory check (modes 0777, 1777, 0755 accepted); `-L` refuses a `tmux-<uid>` directory with any "other" permission bit. `-S` with a missing parent directory exits 0 without a server (3.5 prints an error, 3.2 nothing); a path over 108 bytes fails. | missing-parent stderr only on 3.5 | D26, [Server selection](interface.md#server-selection) |
| `switch-client -c C -t '=S:@W.%P'` and `attach -t '=S:@W.%P'` fail with `can't find pane` or `can't find window` when the pane is not in that window or the window not in that session, leaving the client unchanged. | both | D13, [`focus`](interface.md#focus) |
| `kill-server` leaves the socket file on disk. | both | D26 (a socket file without a server means no server) |

### Spike G: environment scope of `-e`

Run on 2026-10-30 against tmux 3.5a and 3.2a, each on a private `-S` socket with `-f /dev/null` and `SHELL=/bin/sh`; identical results on both.

| Finding | Version notes | Relied on by |
|---|---|---|
| `new-session -e FOO=v` stores `FOO` in the session environment, and a later plain `new-window` in that session inherits it. | both | D10, [Environment](interface.md#environment) |
| `new-window -e` and `respawn-pane -e` set the variable for that pane only; the session environment is unchanged. | both | D10, [Open order](interface.md#open-order) |
| `new-session` without `-e` followed by `respawn-pane -k -e` (separate or chained with `;`) gives the variable only to that pane; later windows are clean. | both | D10, D19 |
| A placeholder `new-session`, then `new-window -e` and a kill of the placeholder window or pane: the session survives with one window at index 1 (a later `new-window` reuses index 0); the variable is only in that pane. | both | D10 (approach not chosen) |
| `new-session -e FOO=v` chained with `set-environment -u FOO`: the first pane keeps `FOO`, the session environment is clean and later windows do not get it. With `-r` instead of `-u` it also works but leaves a `-FOO` removal marker, so later windows get `FOO` removed even when the server has it. | both | D10, [Open order](interface.md#open-order) |
| `env -- PATH=/usr/bin true` runs with that `PATH` (exit 0); `env PATH=/usr/bin -- true` fails with exit 127, treating `--` as the command. | GNU coreutils `env` 8.32 | D33, [Command](interface.md#command) |

## Dependencies

- Runtime: Python 3.12+, `pyyaml` (config), `pydantic` (config validation); tmux 3.2+ at run time.
- Test-only: `jsonschema` and `referencing`, to validate golden `--json` outputs, hook payloads and log records against `schemas/` (resolved by `$id`).
- No awkit tool dependencies.

## Testing

- Unit tests with a fake gateway for every module.
- Integration tests against real tmux in CI (tmux from apt): each test pre-starts its own server with `tmux -f /dev/null -S <path>` on a private socket path and passes `--socket <path>` to termctl, so the server never loads a user's config; a `make verify-floor` target runs them with tmux 3.2 when available locally.
- Golden `--json` files, validated against `schemas/`.
- No `live` tests: nothing termctl touches needs credentials or a model.
