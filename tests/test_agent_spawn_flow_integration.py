"""agent-spawn's flows end to end against real tmux, through termctl's CLI only (#27).

agent-spawn is termctl's one awkit consumer (design.md "Role and layer"). The contract it relies
on is architecture section 5.1 (termctl) and sections 9.3, 9.4 and 9.6, with termctl's own
docs/interface.md as the normative source: ``open`` with the agent's labels (placement is
termctl's config), store the returned id and ``socket_path`` and pass ``--socket`` back on every
later call; ``mark <id> attention`` and ``unmark`` on every status poll; ``focus --json`` and
exec the attach argv; ``close``, which automation never forces (a running pane is refused with
exit 20, ``reason: running``, and exit 10 means already closed), while a human may.

Every step runs the real CLI as its own process (``python -m termctl``), the way a caller
would, and every ``--json`` output, hook payload, log record and audit record is validated
against ``schemas/``. Nothing here imports termctl's internals for the flow itself.

Isolation: each test pre-starts a private server with ``tmux -f /dev/null -S <path>``
(``tmux_server`` in conftest.py), so termctl never starts one; termctl and every client run
with private ``HOME``, ``XDG_CONFIG_HOME`` and ``XDG_STATE_HOME``. Teardown stops exactly that
server with ``kill-server`` on its socket, and a client still running after that is killed
through the ``Popen`` handle of the exact process this test started.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .conftest import TmuxServer, wait_for
from .schemas import validate

pytestmark = pytest.mark.tmux

SRC = str(Path(__file__).resolve().parents[1] / "src")
ID_PATTERN = r"tc-[a-z2-7]{8}"

# The caller's hooks: each writes its payload to $HOOK_DIR/<hook>.json; a pre-hook also returns
# a decision id. Hooks get the caller's full environment, so HOOK_DIR reaches them.
PRE_HOOK = (
    'cat > "$HOOK_DIR/$TERMCTL_HOOK.json"; printf \'{"decision_id": "gate-%s"}\' "$TERMCTL_HOOK"'
)
POST_HOOK = 'cat > "$HOOK_DIR/$TERMCTL_HOOK.json"'

# termctl's config as a user would set it for agent-spawn: agents owned by a workflow go to
# their own session; anything else uses the built-in default placement.
CONFIG = """\
placements:
  agents:
    session: agents
rules:
  - match:
      owner: ticket-work
    placement: agents
"""

# The command an agent window runs: it records its environment and cwd, then waits for the
# test to let it finish and exits 3 (a failure, so --keep-on-exit failed keeps the pane).
AGENT_SCRIPT = (
    'printf "%s\\n" "$AGENT_ID" "$PWD" > started; while [ ! -e finish ]; do sleep 0.1; done; exit 3'
)


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)


@dataclass
class Caller:
    """agent-spawn's side: runs termctl as a process with a private environment, and owns
    the tmux clients it attaches on pseudo-terminals it creates."""

    server: TmuxServer
    root: Path
    clients: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])

    @property
    def state(self) -> Path:
        return self.root / "state" / "termctl"

    @property
    def hooks(self) -> Path:
        return self.root / "hooks"

    def environ(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "LANG": "C.UTF-8",
            "PYTHONPATH": SRC,
            "HOOK_DIR": str(self.hooks),
            **(extra or {}),
        }

    def run(
        self, *argv: str, tty: bool = False, extra_env: dict[str, str] | None = None
    ) -> Outcome:
        """One termctl invocation. With ``tty`` its stdin and stderr are a pseudo-terminal
        this test owns (a human's terminal); stdout is always a pipe."""
        command = [sys.executable, "-m", "termctl", *argv]
        env = self.environ(extra_env)
        if not tty:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
            return Outcome(result.returncode, result.stdout, result.stderr)
        master, slave = pty.openpty()
        try:
            result = subprocess.run(
                command,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=slave,
                env=env,
                timeout=60,
                start_new_session=True,
                check=False,
            )
        finally:
            os.close(slave)
        stderr = _read_available(master)
        os.close(master)
        return Outcome(result.returncode, result.stdout.decode(), stderr)

    def ok(self, *argv: str, schema: str, **kwargs: Any) -> Any:
        """A ``--json`` invocation that must exit 0; its output validated against ``schema``."""
        result = self.run("--json", *argv, **kwargs)
        assert result.code == 0, result.stdout + result.stderr
        out = result.json()
        validate(out, f"termctl/cli/{schema}.json")
        return out

    def failed(self, code: int, *argv: str) -> dict[str, Any]:
        """A ``--json`` invocation that must exit ``code`` with an error object."""
        result = self.run("--json", *argv)
        assert result.code == code, result.stdout + result.stderr
        envelope = result.json()
        validate(envelope, "termctl/cli/error.json")
        return envelope["error"]

    def attach(self, argv: list[str]) -> subprocess.Popen[bytes]:
        """Exec ``argv`` (focus's attach command) on a pseudo-terminal, as the caller would in
        the user's terminal, with the private environment."""
        master, slave = pty.openpty()
        env = {**self.server.env, "TERM": "xterm"}
        proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
        )
        os.close(slave)
        threading.Thread(target=_drain, args=(master,), daemon=True).start()
        self.clients.append(proc)
        return proc

    def hook_payload(self, hook: str, schema: str) -> Any:
        payload = json.loads((self.hooks / f"{hook}.json").read_text(encoding="utf-8"))
        validate(payload, f"termctl/hooks/{schema}.json")
        return payload

    def log(self) -> list[dict[str, Any]]:
        """Every ``log.jsonl`` record, each validated against ``schemas/log.json``."""
        return _records(self.state / "log.jsonl", "termctl/log.json")

    def audit(self) -> list[dict[str, Any]]:
        return _records(self.state / "audit.jsonl", "termctl/audit.json")


def _records(path: Path, schema: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        validate(record, schema)
    return records


def _read_available(fd: int) -> str:
    chunks: list[bytes] = []
    while select.select([fd], [], [], 0)[0]:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)


@pytest.fixture
def caller(tmux_server: TmuxServer, tmp_path: Path) -> Iterator[Caller]:
    for name in ("home", "config/termctl", "state", "hooks", "work"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "config" / "termctl" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    # A private server started with -f /dev/null: termctl never has to start one.
    tmux_server.start("base")
    found = Caller(tmux_server, tmp_path)
    try:
        yield found
    finally:
        tmux_server.raw("kill-server")
        for proc in found.clients:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()  # the exact client this test started
                proc.wait(timeout=10)


def window_name(server: TmuxServer, pane_id: str) -> str:
    result = server.raw("display", "-p", "-t", pane_id, "#{window_name}")
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


def client_panes(server: TmuxServer) -> dict[str, str]:
    """Each attached client's name and the pane it shows."""
    lines = server.raw("list-clients", "-F", "#{client_name} #{pane_id}").stdout.splitlines()
    return dict(line.split(" ", 1) for line in lines)


def canonical(socket_path: str) -> str:
    return os.path.join(os.path.realpath(os.path.dirname(socket_path)), "sock")


def launch(caller: Caller, socket_path: str) -> dict[str, Any]:
    """agent-spawn's launch: open the agent's window with its labels, a name built from
    trusted data, its cwd and environment, the actor and decision id, and the caller's hooks.
    The server comes from termctl's environment here; the output's socket_path is stored."""
    work = caller.root / "work"
    out = caller.ok(
        "open",
        "--name", "KEY-12",
        "--label", "ticket=KEY-12",
        "--label", "owner=ticket-work",
        "--cwd", str(work),
        "--env", "AGENT_ID=agent-1",
        "--keep-on-exit", "failed",
        "--actor", "auto:agent-spawn",
        "--decision-id", "launch-1",
        "--hook", f"pre-open={PRE_HOOK}",
        "--hook", f"post-open={POST_HOOK}",
        "--", "sh", "-c", AGENT_SCRIPT,
        schema="open",
        extra_env={"TERMCTL_SOCKET": socket_path},
    )  # fmt: skip
    return out


def test_agent_spawn_flow(caller: Caller) -> None:
    server = caller.server
    work = caller.root / "work"

    # Placement is termctl's: the labels agent-spawn passes select the rule.
    report = caller.ok(
        "placements", "--label", "ticket=KEY-12", "--label", "owner=ticket-work",
        schema="placements",
    )  # fmt: skip
    assert report["match"]["placement"] == "agents"

    # A preview changes nothing and runs no hooks.
    preview = caller.ok(
        "--socket", server.socket_path, "open", "--dry-run", "--name", "KEY-12",
        "--label", "owner=ticket-work", "--hook", f"pre-open={PRE_HOOK}",
        "--", "sh", "-c", AGENT_SCRIPT,
        schema="open-dry-run",
    )  # fmt: skip
    assert (preview["session"], preview["dry_run"]) == ("agents", True)
    assert not any(caller.hooks.iterdir())

    # --- launch: open, then store the id and socket_path ------------------------------------
    opened = launch(caller, server.socket_path)
    assert re.fullmatch(ID_PATTERN, opened["id"])
    agent_id, socket_path = opened["id"], opened["socket_path"]
    assert socket_path == canonical(server.socket_path)
    assert (opened["name"], opened["display_name"]) == ("KEY-12", "KEY-12")
    assert opened["labels"] == ["owner=ticket-work", "ticket=KEY-12"]
    assert (opened["placement"], opened["session"]) == ("agents", "agents")
    assert (opened["state"], opened["keep_on_exit"], opened["focused"]) == (
        "running",
        "failed",
        False,
    )
    assert isinstance(opened["pid"], int)
    assert opened["decision_ids"] == ["launch-1", "gate-pre-open"]
    pre_open = caller.hook_payload("pre-open", "pre-open")
    assert (pre_open["object_id"], pre_open["env_names"]) == (agent_id, ["AGENT_ID"])
    assert "agent-1" not in (caller.hooks / "pre-open.json").read_text(encoding="utf-8")
    post_open = caller.hook_payload("post-open", "post-open")
    assert post_open["pane"]["id"] == agent_id
    # The command runs directly in the cwd with the --env variable.
    assert wait_for(lambda: (work / "started").exists(), timeout=10)
    assert wait_for(lambda: len((work / "started").read_text().splitlines()) == 2, timeout=10)
    assert (work / "started").read_text().splitlines() == ["agent-1", str(work.resolve())]

    # Another caller's pane, which the rules leave on the default placement.
    other = caller.ok(
        "--socket", socket_path, "open", "--name", "PR#42 review", "--label", "owner=review-run",
        "--actor", "auto:agent-spawn", "--", "sleep", "600",
        schema="open",
    )  # fmt: skip
    assert (other["placement"], other["session"]) == ("default", "termctl")

    # From here on every call passes the stored socket_path back (no TERMCTL_SOCKET, no TMUX).
    def sock(*argv: str) -> tuple[str, ...]:
        return ("--socket", socket_path, *argv)

    # --- show / exists / list -----------------------------------------------------------------
    assert caller.ok(*sock("exists", agent_id), schema="exists")["exists"] is True
    shown = caller.ok(*sock("show", agent_id), schema="show")
    assert {key: shown[key] for key in ("id", "pane_id", "window_id", "socket_path")} == {
        key: opened[key] for key in ("id", "pane_id", "window_id", "socket_path")
    }
    listed = caller.ok(*sock("list", "--label", "owner=ticket-work"), schema="list")
    assert [pane["id"] for pane in listed["panes"]] == [agent_id]
    every = caller.ok(*sock("list"), schema="list")
    assert {pane["id"] for pane in every["panes"]} == {agent_id, other["id"]}

    # --- supervise: the status marker, set on every poll ------------------------------------
    marked = caller.ok(*sock("mark", agent_id, "attention"), schema="mark")
    assert (marked["changed"], marked["display_updated"], marked["marker"]) == (
        True,
        True,
        "attention",
    )
    assert marked["display_name"] == "🔔 KEY-12" == window_name(server, opened["pane_id"])
    again = caller.ok(*sock("mark", agent_id, "attention"), schema="mark")
    assert (again["changed"], again["display_name"]) == (False, "🔔 KEY-12")

    renamed = caller.ok(*sock("rename", agent_id, "KEY-12 fix"), schema="rename")
    assert (renamed["name"], renamed["marker"]) == ("KEY-12 fix", "attention")
    assert renamed["display_name"] == "🔔 KEY-12 fix" == window_name(server, opened["pane_id"])

    unmarked = caller.ok(*sock("unmark", agent_id), schema="unmark")
    assert (unmarked["changed"], unmarked["marker"], unmarked["marker_text"]) == (
        True,
        None,
        None,
    )
    assert unmarked["display_name"] == "KEY-12 fix" == window_name(server, opened["pane_id"])
    assert caller.ok(*sock("unmark", agent_id), schema="unmark")["changed"] is False

    # --- labels -------------------------------------------------------------------------------
    added = caller.ok(*sock("label", "add", agent_id, "pr=42"), schema="label-add")
    assert added["labels"] == ["owner=ticket-work", "pr=42", "ticket=KEY-12"]
    assert [
        p["id"] for p in caller.ok(*sock("list", "--label", "pr=42"), schema="list")["panes"]
    ] == [agent_id]
    removed = caller.ok(*sock("label", "remove", agent_id, "pr=42"), schema="label-remove")
    assert removed["labels"] == ["owner=ticket-work", "ticket=KEY-12"]

    # --- attach: focus --json from the user's terminal, then exec the argv -----------------
    focus = caller.ok(*sock("focus", agent_id), schema="focus", tty=True)
    session_id = server.raw("display", "-p", "-t", opened["pane_id"], "#{session_id}").stdout
    assert focus["action"] == "attach" and focus["client"] is None
    assert focus["argv"][1:] == [
        "-S", socket_path, "attach", "-E",
        "-t", f"={session_id.strip()}:{opened['window_id']}.{opened['pane_id']}",
    ]  # fmt: skip
    assert os.path.realpath(focus["argv"][0]) == server.tmux_bin
    caller.attach(focus["argv"])
    assert wait_for(lambda: len(client_panes(server)) == 1, timeout=10)
    ((client, shown_pane),) = client_panes(server).items()
    assert shown_pane == opened["pane_id"]

    # With a client attached, focus switches it (no attach), and a dry run changes nothing.
    switched = caller.ok(*sock("focus", other["id"]), schema="focus")
    assert (switched["action"], switched["client"], switched["dry_run"]) == (
        "switched",
        client,
        False,
    )
    assert client_panes(server) == {client: other["pane_id"]}
    preview_focus = caller.ok(*sock("focus", agent_id, "--dry-run"), schema="focus")
    assert (preview_focus["client"], preview_focus["dry_run"]) == (client, True)
    assert client_panes(server) == {client: other["pane_id"]}

    # --- teardown: automation never forces; a running pane is refused ----------------------
    refused = caller.failed(20, *sock("close", agent_id, "--actor", "auto:agent-spawn"))
    assert (refused["code"], refused["reason"]) == ("refused", "running")
    assert caller.ok(*sock("exists", agent_id), schema="exists")["exists"] is True

    # The agent's command exits (non-zero): --keep-on-exit failed keeps a dead pane.
    (work / "finish").touch()

    def dead() -> bool:
        return caller.ok(*sock("show", agent_id), schema="show")["state"] == "dead"

    assert wait_for(dead, timeout=15)
    final = caller.ok(*sock("show", agent_id), schema="show")
    assert (final["exit_status"], final["exit_signal"]) == (3, None)
    # The other pane still runs: --dead lists the agent's alone.
    dead_panes = caller.ok(*sock("list", "--dead"), schema="list")["panes"]
    assert [pane["id"] for pane in dead_panes] == [agent_id]

    closed = caller.ok(
        *sock(
            "close", agent_id, "--actor", "auto:agent-spawn", "--decision-id", "teardown-1",
            "--hook", f"pre-close={PRE_HOOK}", "--hook", f"post-close={POST_HOOK}",
        ),
        schema="close",
    )  # fmt: skip
    assert closed["id"] == agent_id and closed["socket_path"] == socket_path
    assert (closed["pane_closed"], closed["was_running"], closed["forced"]) == (True, False, False)
    assert (closed["signals_sent"], closed["pid_mismatch"]) == ([], False)
    assert closed["decision_ids"] == ["teardown-1", "gate-pre-close"]
    pre_close = caller.hook_payload("pre-close", "pre-close")
    assert (pre_close["object_id"], pre_close["pane"]["state"]) == (agent_id, "dead")
    post_close = caller.hook_payload("post-close", "post-close")
    assert post_close["result"] == closed

    # Gone now: exists says so, and a second close is exit 10 ("already closed").
    gone = caller.run("--json", *sock("exists", agent_id))
    assert gone.code == 10
    validate(gone.json(), "termctl/cli/exists.json")
    assert gone.json()["exists"] is False
    assert caller.failed(10, *sock("close", agent_id))["code"] == "not_found"
    assert caller.failed(10, *sock("mark", agent_id, "attention"))["code"] == "not_found"

    # --- the records: one per logged invocation, none for no-ops and dry runs -------------
    records = caller.log()
    assert [r for r in records if r["event"] in ("warning", "hook_failed")] == []
    summary = [
        (r["event"], r["result"], r.get("exit_code"), r["id"], r["socket_path"]) for r in records
    ]
    other_id = other["id"]
    assert summary == [
        ("open", "ok", 0, agent_id, socket_path),
        ("open", "ok", 0, other_id, socket_path),
        ("mark", "ok", 0, agent_id, socket_path),
        ("rename", "ok", 0, agent_id, socket_path),
        ("unmark", "ok", 0, agent_id, socket_path),
        ("label_add", "ok", 0, agent_id, socket_path),
        ("label_remove", "ok", 0, agent_id, socket_path),
        ("focus", "ok", 0, agent_id, socket_path),
        ("focus", "ok", 0, other_id, socket_path),
        ("close", "refused", 20, agent_id, socket_path),
        ("close", "ok", 0, agent_id, socket_path),
        ("close", "error", 10, None, socket_path),
        ("mark", "error", 10, None, socket_path),
    ]
    first = records[0]
    assert (first["actor"], first["decision_ids"]) == (
        "auto:agent-spawn",
        ["launch-1", "gate-pre-open"],
    )
    assert [hook["hook"] for hook in first["hooks"]] == ["pre-open", "post-open"]
    assert [r["action"] for r in records if r["event"] == "focus"] == ["attach", "switched"]
    # Nothing was forced or overridden, so nothing was audited.
    assert caller.audit() == []


def test_an_agent_whose_window_closed_is_already_closed(caller: Caller) -> None:
    """The default --keep-on-exit never: the window goes when the agent's command exits, and
    a later teardown's close is exit 10, which callers treat as already closed."""
    socket_path = canonical(caller.server.socket_path)
    work = caller.root / "work"
    opened = caller.ok(
        "--socket", socket_path, "open", "--name", "KEY-13", "--label", "ticket=KEY-13",
        "--cwd", str(work), "--actor", "auto:agent-spawn",
        "--", "sh", "-c", "while [ ! -e finish ]; do sleep 0.1; done",
        schema="open",
    )  # fmt: skip
    assert opened["keep_on_exit"] == "never"
    (work / "finish").touch()

    def gone() -> bool:
        return caller.run("--json", "--socket", socket_path, "exists", opened["id"]).code == 10

    assert wait_for(gone, timeout=15)
    exists = caller.run("--json", "--socket", socket_path, "exists", opened["id"])
    assert exists.code == 10
    validate(exists.json(), "termctl/cli/exists.json")
    assert exists.json()["exists"] is False
    assert caller.ok("--socket", socket_path, "list", schema="list")["panes"] == []
    error = caller.failed(10, "--socket", socket_path, "close", opened["id"])
    assert error["code"] == "not_found"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads /proc (Linux)")
def test_a_human_teardown_forces_a_running_agent(caller: Caller) -> None:
    """A human may force what automation never does: the pane is killed, the recorded process
    ends, and the forced close is audited."""
    socket_path = canonical(caller.server.socket_path)
    opened = caller.ok(
        "--socket", socket_path, "open", "--name", "KEY-14", "--label", "owner=ticket-work",
        "--actor", "auto:agent-spawn", "--", "sleep", "600",
        schema="open",
    )  # fmt: skip
    pid = opened["pid"]
    assert isinstance(pid, int)
    # Automation's gate: --force needs a TTY or a human actor.
    usage = caller.failed(2, "--socket", socket_path, "close", opened["id"], "--force")
    assert usage["code"] == "usage"

    closed = caller.ok(
        "--socket", socket_path, "close", opened["id"], "--force", "--grace", "2s",
        "--actor", "human:tester", "--decision-id", "human-ok",
        schema="close",
    )  # fmt: skip
    assert (closed["pane_closed"], closed["was_running"], closed["forced"]) == (True, True, True)
    assert (closed["pid_mismatch"], closed["exited"]) == (False, True)
    assert not Path(f"/proc/{pid}").exists() or "Z" in _proc_state(pid)
    assert caller.failed(10, "--socket", socket_path, "show", opened["id"])["code"] == "not_found"

    [audit] = caller.audit()
    assert (audit["action"], audit["target"], audit["actor"]) == (
        "close",
        opened["id"],
        "human:tester",
    )
    assert (audit["force"], audit["decision_id"], audit["result"]) == (True, ["human-ok"], closed)
    assert audit["inputs"]["pid"] == pid
    closes = [r for r in caller.log() if r["event"] == "close"]
    # The refused --force was a usage error (check order step 1), which is never logged.
    assert [(r["result"], r.get("exit_code"), r["force"]) for r in closes] == [("ok", 0, True)]


def _proc_state(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return "Z"
    return stat.rsplit(")", 1)[1].split()[0]
