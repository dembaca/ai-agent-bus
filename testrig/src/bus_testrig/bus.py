"""The bus itself, as a process, and a run that measures it.

This harness used to measure only its own echo loop and said so: no code path
here started the bus (review point 1). That is what this module adds. The real
MCP server — `python -m mattermost_ai_agent_bus`, the entry point
`hosted.main` picks — runs as a subprocess, speaks JSON-RPC over its stdin and
stdout and is pointed at the stub through the proxy, so the faults the echo run
injects are injected into the bus's own sockets.

The proxy is there in **every** run, with `fault=None` for the baseline: a run
without a fault and a run with one then take the same path, and their numbers
compare directly (review point 4). Before that the baseline dialled the stub and
its relay counters were empty, so "the fault changed nothing" could not be told
from "the fault was not in the path".

`bus_run` sends with `post_message` and receives with `wait_for_events`: the
two tools an agent actually uses, driven through the server, its inbox and the
tool boundary the acceptance talks about, not around them. Two processes, not
one: a post that arrives in the sender's own inbox is dropped as its own
(`websocket_inbox.classify_post`), so the receiver is another bot, and the stub
tells the two apart by the token each presents.

What this still cannot measure is what a run here does not stage: a role
switch. The coordinator rows of the plan — a hard stop, the crown passing, a
human posting 100 times under fault — stay `not_yet_measurable` in
`measurands.toml`, each with that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .proxy import GatewayProxy, ProxyFault
from .runner import Run
from .stub import MattermostStub, posted_frame

# testrig/src/bus_testrig/bus.py -> the repository root
REPO = Path(__file__).resolve().parents[3]
MCP = REPO / "mcp"

# A start that hangs is a finding, not something to wait out: the server answers
# `initialize` before it touches a bot, so 30 s is already generous.
START_SEC = 30.0
STOP_SEC = 5.0
# The server's last stderr lines a report carries (review point 15: a number is
# never read without what produced it).
TAIL = 200

# How often a reader whose wait failed tries again, and how long it waits
# between tries: the bus's inbox reconnects and backfills on its next
# wait_for_events, so a single failure must not end the read (review point 3).
RECONNECTS = 40
RETRY_SEC = 0.25

# How long a run waits, after its last send, for *any* frame to arrive before it
# calls the path broken instead of slow (review of #35).
#
# Derived, not guessed: the settle window is `max(stall, reorder, send_interval)
# + 0.4`, and each of those is the run's own argument, so the settle is known
# before the run starts. This deadline is `10 x settle + 5 s`; the multiplier
# covers a slow runner's scheduling and the fixed 5 s covers one
# `wait_for_events` round trip. Without it, a run whose receiver never gets
# anything waits until the job's own timeout, and a mutation that breaks
# delivery shows up as a hang instead of a failed assertion.
def delivery_deadline(settle: float) -> float:
    return 10.0 * settle + 5.0


PROTOCOL = "2025-06-18"
# Two tokens, two bots: the stub answers `users/me` with the one the bearer
# names. The ids are well-formed Mattermost ids and belong to no real bot.
SENDER_TOKEN = "testrig-sender"
RECEIVER_TOKEN = "testrig-receiver"
SENDER_ID = "s" * 26
RECEIVER_ID = "r" * 26

_ERROR = re.compile(r"Traceback|Error|Exception|failed|refused|raised")


class McpError(RuntimeError):
    """The server answered, and the answer says the call failed."""


def _clean_env(base_url: str, token: str, state_dir: str) -> dict[str, str]:
    """The server's environment: the rig's URL and nothing that reaches home.

    Every inherited `MM_*`/`MATTERMOST_*` goes — this machine runs a real bus,
    and a server started for a run must not touch it — as do the corral and
    Claude Code markers, which would make the server read a state directory and
    a token that are not this run's. `XDG_STATE_HOME` is a throwaway directory,
    so session and release files land there and nowhere else.

    `HTTP(S)_PROXY`/`ALL_PROXY` go too, with their lower-case forms (follow-up
    review point 4): the stub and the proxy are loopback, and `httpx` — which
    the server itself uses for every REST call — reads those variables and
    would send the run's own traffic through a proxy that is not the stub.
    That failed the run at `get_me` before it measured anything, and the
    server's client is not this rig's to change.
    """
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("MM_", "MATTERMOST_", "CORRAL_", "CLAUDE"))
           and name != "XDG_STATE_HOME"
           and name.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update({
        "MM_CHAT_URL": base_url,
        "MM_BOT_TOKEN": token,
        "MM_TEAM": "agents",
        "MM_CHANNEL": "agents",
        "MM_CHANNEL_PUSH": "0",
        "PYTHONPATH": str(MCP / "src"),
        "XDG_STATE_HOME": state_dir,
    })
    return env


class Unavailable(RuntimeError):
    """No interpreter here imports the bus: the run cannot start, and says why."""


_interpreter: list[str] | None = None
_interpreting: asyncio.Lock | None = None


def interpreter() -> list[str]:
    """The command that runs the bus, found by trying, not by assuming.

    `mcp/pyproject.toml` installs the package editable into `mcp/.venv`, and CI
    has only `uv`. This interpreter — the testrig's — has neither, so each
    candidate is asked whether it can import the package, bounded by a timeout;
    the first that can is used. A run under an interpreter without `mcp` would
    otherwise fail as a child that died with a traceback on stderr, which reads
    like a bus that never started.

    There is deliberately no `uv run` candidate (review point at bus.py:118):
    probing through it synced `mcp/` as a side effect, so a probe that failed
    left an empty `mcp/.venv` behind and every later run reused it. The
    environment `uv sync` would make is `mcp/.venv`, which the second candidate
    already names — so a host without it is told to sync, not quietly given one.
    """
    global _interpreter
    if _interpreter is not None:
        return _interpreter
    # An explicit interpreter wins and is the only candidate: a host whose
    # layout the probe does not know names its own, and a run can be pointed at
    # one that cannot import the bus to see what it does then.
    pinned = os.environ.get("BUS_TESTRIG_PYTHON")
    candidates = [[pinned or sys.executable]]
    venv = MCP / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not pinned and venv.exists():
        candidates.append([str(venv)])
    probe_env = {name: value for name, value in os.environ.items()
                 if not name.startswith(("MM_", "MATTERMOST_", "CORRAL_", "CLAUDE"))}
    probe_env["PYTHONPATH"] = str(MCP / "src")
    for argv in candidates:
        try:
            status = subprocess.run(argv + ["-c", "import mattermost_ai_agent_bus, mcp"],
                                    cwd=str(MCP), env=probe_env, timeout=60, check=False,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL).returncode
        except (OSError, subprocess.TimeoutExpired):
            continue
        if status == 0:
            _interpreter = argv
            return argv
    raise Unavailable("no interpreter here imports mattermost_ai_agent_bus: "
                      "run `uv sync --directory mcp` first")


async def interpreter_async() -> list[str]:
    """`interpreter` off the loop (review point at bus.py:118).

    The probe is synchronous and may wait 60 s per candidate; running it on the
    event loop blocked every other task of the run — including another server's
    reader — for that whole time. One probe at a time: two runs starting
    together must not probe twice.
    """
    global _interpreting
    if _interpreter is not None:
        return _interpreter
    if _interpreting is None:
        _interpreting = asyncio.Lock()
    async with _interpreting:
        if _interpreter is not None:
            return _interpreter
        return await asyncio.to_thread(interpreter)


def forget_interpreter() -> None:
    """Forget the probed interpreter: a test that simulates one implacable
    interpreter set must not leak into the next test."""
    global _interpreter
    _interpreter = None


class BusProcess:
    """One real MCP server, over newline-delimited JSON-RPC on its pipes.

    Only what a client of the bus needs: `initialize`, `tools/list` and
    `tools/call`. Requests and notifications are told apart by `id` — the
    protocol does that — and the server's own requests (`roots/list`, `ping`)
    are answered rather than read as replies. stderr is drained on its own
    task: a pipe nobody reads fills and stops the child, which would look
    exactly like a hang.
    """

    def __init__(self, base_url: str, *, token: str, tail: int = TAIL) -> None:
        self.base_url = base_url
        self.token = token
        self.tools: list[str] = []
        self._tail = tail
        self._lines: list[str] = []
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._state: str = ""

    async def __aenter__(self) -> BusProcess:
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()

    @property
    def stderr_tail(self) -> list[str]:
        """The last lines the server wrote to stderr, in order."""
        return list(self._lines)

    @property
    def errors(self) -> list[str]:
        """The lines that name a failure; `httpx`'s access log is not one."""
        return [line for line in self._lines
                if _ERROR.search(line) and not line.startswith("HTTP Request:")]

    async def start(self) -> None:
        # The probe first, then the temp dir (review point at bus.py:176): a
        # mkdtemp before a probe that fails left one directory behind per
        # failed run.
        argv = await interpreter_async() + ["-m", "mattermost_ai_agent_bus"]
        self._state = tempfile.mkdtemp(prefix="bus-testrig-")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(MCP), env=_clean_env(self.base_url, self.token, self._state),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=4 << 20,
                # A process group of its own: the server starts children on some
                # hosts, and killing only the pid left them (review point at
                # bus.py:114).
                start_new_session=True)
        except OSError as exc:
            shutil.rmtree(self._state, ignore_errors=True)
            self._state = ""
            raise RuntimeError(f"the bus did not start: {exc}") from exc
        except BaseException:
            # A cancel while the spawn is under way (follow-up review point 6):
            # the directory `mkdtemp` just made has no server to clean it up,
            # because `stop` is never reached. Every other way out of `start`
            # already removes it.
            shutil.rmtree(self._state, ignore_errors=True)
            self._state = ""
            raise
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain())
        try:
            answer = await self._rpc("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "bus-testrig", "version": "0"},
            }, timeout=START_SEC)
            if "capabilities" not in answer:
                raise RuntimeError(f"initialize answered without capabilities: {answer}")
            await self._notify("notifications/initialized")
            listed = await self._rpc("tools/list", {}, timeout=START_SEC)
            self.tools = [str(tool.get("name")) for tool in listed.get("tools", [])]
            if not self.tools:
                raise RuntimeError(f"tools/list is empty: {listed}")
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """End the server and reap it: closing stdin is the protocol's own way
        out; a SIGKILL is what remains when the server does not take it. No
        orphan is left for the machine to inherit.

        The group is killed, not the pid (review point at bus.py:114): a child
        the server started would otherwise outlive it. The wait and the kill sit
        in `try/finally`, so a `stop` cancelled half way still takes the child
        down, and `self._proc` is dropped only once that is done — a second
        `stop` after an interrupted one still has a process to kill. stderr is
        read to EOF before the reader ends (review point at bus.py:210): the
        lines the server wrote on its way out are the ones a failing run needs.
        """
        try:
            proc = self._proc
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(BrokenPipeError, ConnectionError):
                    if proc.stdin is not None:
                        proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), STOP_SEC)
                except asyncio.TimeoutError:
                    await self._kill(proc)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), STOP_SEC)
        finally:
            proc = self._proc
            if proc is not None and proc.returncode is None:
                await self._kill(proc)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), STOP_SEC)
            self._proc = None
            await self._finish_readers()
            self._pending.clear()
            # The state directory is this run's alone (a throwaway
            # XDG_STATE_HOME); leaving it behind would grow the machine's temp
            # directory by one per server, which a 10-run-per-fault plan
            # multiplies.
            if self._state:
                shutil.rmtree(self._state, ignore_errors=True)
                self._state = ""

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the server's whole process group, or the pid when there is
        no group of its own (Windows has none)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()

    async def _finish_readers(self) -> None:
        """Drain stdout and stderr to EOF, then end them: cancelling the stderr
        reader first lost the child's last lines (review point at bus.py:210).
        Bounded, so a pipe a grandchild still holds cannot hang the run; a
        reader that failed is already over and its exception is not this call's
        to raise."""
        pending = [task for task in (self._reader, self._stderr) if task is not None]
        if pending:
            _, still = await asyncio.wait(pending, timeout=STOP_SEC)
            for task in still:
                task.cancel()
            if still:
                await asyncio.wait(still, timeout=STOP_SEC)
        self._reader = self._stderr = None

    async def call(self, tool: str, **args: Any) -> Any:
        """One `tools/call`, the tool's own JSON as the server wrote it.

        A tool that fails is raised, not returned: the server wraps the failure
        in `isError`, and a run that read that as a result would report frames
        it never sent (review point 8).
        """
        answer = await self._rpc("tools/call", {"name": tool, "arguments": args},
                                 timeout=_timeout_for(tool, args))
        text = _text_of(answer)
        if answer.get("isError"):
            raise McpError(f"{tool}: {text or 'no text'}")
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {"text": text}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _rpc(self, method: str, params: dict[str, Any],
                   timeout: float) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future())
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": params})
        try:
            answer = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{method}: no answer within {timeout:.0f} s; "
                               f"stderr: {self.errors[-3:]}") from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in answer:
            raise McpError(f"{method}: {answer['error']}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method}: result is not an object: {result!r}")
        return result

    async def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("the bus is not running")
        proc.stdin.write((json.dumps(message) + "\n").encode())
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            raise RuntimeError(f"the bus's stdin is closed: {exc}") from exc

    async def _read(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # A message longer than the reader's limit: the server cannot
                # be talked to any more, and a truncated read must not pass as
                # a reply.
                self._fail_all("the server wrote a message too long to read")
                return
            if not line:
                self._fail_all("the bus's stdout closed")
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                await self._on_request(message)
            elif "id" in message:
                future = self._pending.get(message["id"])
                if future is not None and not future.done():
                    future.set_result(message)

    async def _on_request(self, message: dict[str, Any]) -> None:
        """A request from the server, not a reply. `roots/list` gets an empty
        list: the run's workspace plays no part in what it measures."""
        if "id" not in message:
            return  # a notification: nothing to answer
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if message.get("method") == "roots/list":
            reply["result"] = {"roots": []}
        elif message.get("method") == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601,
                              "message": f"no method {message.get('method')!r}"}
        with contextlib.suppress(RuntimeError):
            await self._write(reply)

    def _fail_all(self, why: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError(f"{why}; stderr: {self.errors[-3:]}"))

    async def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self._lines.append(line.decode("utf-8", "replace").rstrip())
            del self._lines[:-self._tail]


def _posted_text(post: dict[str, Any], known: dict[str, int]) -> str:
    """The text a reader can key on, as the send wrote it.

    `slim.cut_item` shortens a long message and ends it with where the rest is,
    so a post read back is not byte-identical to the one sent. A run's messages
    are short, but the reader must not depend on that: the *longest* registered
    text this post starts with is the send it came from — the shortest would
    map `bus-run 10` to the send `bus-run 1`.
    """
    message = str(post.get("message") or "")
    if message in known:
        return message
    candidates = [text for text in known
                  if text and message.startswith(text[:200])]
    return max(candidates, key=len) if candidates else message


def _arrivals(events: Any, by_id: dict[str, int],
              by_text: dict[str, int]) -> list[int]:
    """The sends a `wait_for_events` answer reports as arrived, in order.

    The call site of the fallback, in a function so that a test drives it with
    the answer `receive()` gets. Testing `_match_seq` alone proved the function
    and not its use: reverting this loop to an id-only match kept the whole
    suite green (review of #25), which is the one thing a test of a fallback
    must not allow.
    """
    out: list[int] = []
    for item in events if isinstance(events, list) else []:
        post = item.get("post") if isinstance(item, dict) else None
        if not isinstance(post, dict):
            continue
        seq = _match_seq(post, by_id, by_text)
        if seq is not None:
            out.append(seq)
    return out


def _match_seq(post: dict[str, Any], by_id: dict[str, int],
               by_text: dict[str, int]) -> int | None:
    """Which send a delivered post came from: its id when the send has returned,
    else the text this run wrote for it.

    One function so that the fallback is checkable on its own (review of #25).
    Watching a whole run cannot prove it: by the time an arrival is counted the
    id is registered, so the id path matches and the fallback might as well be
    absent — the earlier version of the test passed with it removed.
    """
    seq = by_id.get(str(post.get("id")))
    if seq is not None:
        return seq
    return by_text.get(_posted_text(post, by_text))


def _text_of(answer: dict[str, Any]) -> str:
    content = answer.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(str(part.get("text") or "") for part in content
                   if isinstance(part, dict))


def _timeout_for(tool: str, args: dict[str, Any]) -> float:
    """The call's own deadline: a wait is asked for a timeout, and the client
    must outlast it, or a normal empty wait reads as a hung server."""
    if tool == "wait_for_events":
        try:
            return float(args.get("timeout_sec") or 60.0) + 20.0
        except (TypeError, ValueError):
            return 80.0
    return 60.0


async def bus_run(count: int = 100, fault: ProxyFault | None = None,
                  send_interval: float = 0.0) -> Run:
    """Post `count` messages through the bus and measure what the bus received.

    One server posts (`post_message`), another receives (`wait_for_events`);
    the stub turns each post it created into the `posted` frame Mattermost
    would have broadcast, so the frame is the post's own JSON and not one the
    harness invented. `send` is recorded before the tool call and `arrive`
    when the receiver's wait returns the post, which is the measurement point
    the plan proposes.

    An arrival is matched by post id when the send is back, and by the text the
    send wrote when it is not (follow-up review point 3): after a `cut` the
    bus's catch-up read can hand a post over while `post_message` still waits
    for its answer, and an id-only match counted that post twice — once here as
    lost, once in the read as unknown.

    A run whose socket died still returns its numbers: the loss is what the
    acceptance compares, and the run's `tail` and `closed` say why the rest is
    missing instead of hiding it.
    """
    run = Run(name="bus-run", count=count, target="bus",
              fault=fault.kind if fault is not None else "none",
              fault_params=({"every": fault.every, "after": fault.after,
                             "stall_sec": fault.stall_sec,
                             "reorder_max_wait": fault.reorder_max_wait}
                            if fault is not None else {}),
              send_interval=send_interval)
    # The settle window must outlast every silence the fault can produce, and
    # it starts only when sending is done: a window that runs during sending
    # reports frames still in flight as losses.
    stall = fault.stall_sec if fault is not None and fault.kind == "stall" else 0.0
    reorder_wait = fault.reorder_max_wait if fault is not None else 0.0
    settle = max(stall, reorder_wait, send_interval) + 0.4
    limit = delivery_deadline(settle)
    run.started = time.monotonic()
    async with contextlib.AsyncExitStack() as stack:
        stub = await stack.enter_async_context(MattermostStub())
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        stub.identify(RECEIVER_TOKEN, RECEIVER_ID, "agent-receiver")
        # Always through the proxy, with `fault=None` for the baseline (review
        # point 4): the two runs then take the same path, and the relay's
        # counters are there to compare instead of missing from the baseline.
        proxy = await stack.enter_async_context(GatewayProxy(stub.url, fault))
        sender = BusProcess(proxy.url, token=SENDER_TOKEN)
        receiver = BusProcess(proxy.url, token=RECEIVER_TOKEN)
        await stack.enter_async_context(sender)
        await stack.enter_async_context(receiver)
        sent_done: float | None = None
        last_arrival = time.monotonic()
        # id -> seq for the posts the stub created, and text -> seq for the same
        # sends. The text is the one that is always there in time (follow-up
        # review point 3): it is registered before the send, so a post the
        # receiver's catch-up read hands over while `post_message` is still in
        # flight is counted as arrived. Its id is not in `seq_of` until that
        # call returns, and matching on the id alone counted it as lost.
        seq_of: dict[str, int] = {}
        seq_of_text: dict[str, int] = {}
        reader: asyncio.Task[None] | None = None
        try:
            # Nothing here needs a registered bot: the stub answers the calls
            # these tools make, and `get_me` is the harmless read that proves
            # the server is up and talking to the rig.
            await sender.call("get_me")
            await receiver.call("get_me")
            # The inbox is opened here, before the first frame: it connects on
            # the first wait, and a frame pushed before that reaches nobody.
            # A fault that eats the socket's own handshake leaves no inbox at
            # all — that is a measurement (everything is lost, and `closed`
            # says why), not a reason to abort the run.
            socket_ok = await _open_inbox(receiver, run)
            if socket_ok:
                socket_ok = await _await_socket(stub, receiver, run, timeout=START_SEC)

            async def receive() -> None:
                """Read continuously, never waiting for the sender first — the
                error the echo run's review point 7 names. It ends only when
                sending is done **and** the line has been quiet for `settle`
                since the later of the last send and the last arrival.

                A failed wait is retried a bounded number of times (review
                point 3): the bus's inbox reconnects and backfills on its next
                wait, so ending the reader at the first error would report
                everything after a `cut` as lost although the bus recovers it.
                """
                nonlocal last_arrival
                failures = 0
                while True:
                    try:
                        events = await receiver.call("wait_for_events", timeout_sec=0.25)
                    except (McpError, RuntimeError, TimeoutError) as exc:
                        run.closed = run.closed or f"{type(exc).__name__}: {exc}"[:300]
                        failures += 1
                        if failures > RECONNECTS:
                            return
                        await asyncio.sleep(RETRY_SEC)
                        continue
                    failures = 0
                    for seq in _arrivals(events, seq_of, seq_of_text):
                        run.arrive(seq)
                        last_arrival = time.monotonic()
                    if sent_done is not None:
                        quiet = time.monotonic() - max(last_arrival, sent_done)
                        if quiet > settle:
                            return
                        # Sent, and nothing has come back at all: the path is
                        # broken, not slow. Say so in one line and end the run
                        # rather than wait for the job's own timeout (review of
                        # #35: a mutation that broke delivery hung instead of
                        # failing).
                        if not run.received and quiet > limit:
                            run.closed = (
                                f"nothing arrived within {limit:.1f}s of the last "
                                f"send: {run.sent_frames} frame(s) sent, none received")
                            return

            if socket_ok:
                reader = asyncio.create_task(receive())
            for seq in range(count):
                text = f"bus-run {seq}"
                # Registered before the send (follow-up review point 3): the
                # catch-up read can hand this post over while `post_message` is
                # still waiting for its answer.
                seq_of_text[text] = seq
                run.send(seq)
                post: Any = None
                try:
                    post = await sender.call("post_message", message=text)
                except (McpError, RuntimeError, TimeoutError, OSError) as exc:
                    run.closed = run.closed or (
                        f"post_message failed: {type(exc).__name__}: {exc}")[:300]
                if not isinstance(post, dict) or not post.get("id"):
                    # A send that failed or answered without an id never left
                    # the harness: it is neither sent nor lost (review point 5,
                    # the echo run's `run.sent.pop` at runner.py:246).
                    run.sent.pop()
                    seq_of_text.pop(text, None)
                    if not run.closed:
                        run.closed = f"post_message answered without an id: {post!r}"[:300]
                    break
                run.sent_frames += 1
                created = stub.post_of(str(post["id"]))
                seq_of[str(created["id"])] = seq
                # The server may restyle the text (`a2a.lead`, `a2a.styled`):
                # the post as written is the key the reader matches.
                seq_of_text.setdefault(str(created.get("message") or ""), seq)
                if await stub.push(posted_frame(created)) == 0 and not run.closed:
                    run.closed = "no open socket when a posted frame was pushed"
                if send_interval:
                    await asyncio.sleep(send_interval)
                else:
                    await asyncio.sleep(0)  # let the reader run
            sent_done = time.monotonic()
            if reader is not None:
                await reader
        except (McpError, RuntimeError, TimeoutError, OSError) as exc:
            if not run.closed:
                run.closed = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            # A reader left running past this point would keep calling into a
            # server the stack is about to stop; cancelled and awaited here, it
            # cannot outlive the run.
            if reader is not None and not reader.done():
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            run.ended = time.monotonic()
            run.relay = {"received": proxy.received, "forwarded": proxy.forwarded,
                         "altered": proxy.altered}
            doubled = proxy.forwarded - proxy.received
            if doubled > 0 and fault is not None and fault.kind == "duplicate":
                # The wire carried the frame twice; the bus's inbox hands
                # each post over once (`websocket_inbox._seen_ids`). That is
                # the measurement the acceptance wants ("nothing handled
                # twice"), and it is a different number from the wire's.
                run.notes.append(
                    f"the relay delivered {doubled} frame(s) more than once; the bus "
                    "handed each post over once (duplicates are counted at the tool "
                    "boundary, not on the wire)")
            if fault is not None and fault.kind in ("cut", "halfopen"):
                # Neither fault is a plain delivery loss: the inbox reconnects
                # and backfills after a cut, so what this number counts is the
                # catch-up read as well as the socket, and a halfopen line
                # carries nothing until the client's own heartbeat gives up.
                run.notes.append(
                    f"{fault.kind} is not a socket-delivery measurement: the loss counts "
                    "the bus's catch-up read after a reconnect, or a frozen line's "
                    "silence, not frames the socket dropped one by one")
            if stub.closed:
                run.notes.append(f"the stub's socket: {stub.closed}")
            if run.unmeasured:
                # The report's own words for "these numbers compare with
                # nothing" (review point 2): `loss` here is every frame.
                run.notes.append(f"{run.unmeasured}; loss counts every frame and "
                                 "is not a delivery measurement")
            if stub.unknown:
                run.unknown = list(stub.unknown)
                run.notes.append(f"the stub did not know {len(stub.unknown)} call(s): "
                                 f"{stub.unknown[:5]}")
            run.tail = (receiver.errors or sender.errors)[-15:]
        return run


async def _open_inbox(receiver: BusProcess, run: Run) -> bool:
    """The first wait, which is what makes the bus dial its socket; false when
    that failed (a fault ate the handshake), with the reason on the run."""
    try:
        await receiver.call("wait_for_events", timeout_sec=0.01)
    except (McpError, RuntimeError, TimeoutError) as exc:
        run.closed = f"inbox never opened: {type(exc).__name__}: {exc}"[:300]
        run.unmeasured = "the receiver's inbox never opened"
        return False
    return True


async def _await_socket(stub: MattermostStub, receiver: BusProcess, run: Run, *,
                        timeout: float) -> bool:
    """Wait until the bus's socket is open (it dials on its first wait, so a
    frame pushed before that reaches nobody). An event, not a 10 ms poll
    (review point at bus.py:523): the wait costs nothing and wakes at once.
    False when none opened in time: the stub still answers the sender, so the
    run's numbers are its loss, and `closed` says the inbox never connected."""
    if await stub.wait_for_socket(timeout):
        return True
    run.closed = run.closed or (
        f"the bus opened no socket within {timeout:.0f} s; "
        f"stderr: {receiver.errors[-3:]}")
    run.unmeasured = "the receiver's socket never opened"
    return False
