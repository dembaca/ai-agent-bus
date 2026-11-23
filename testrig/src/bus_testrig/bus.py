"""The bus itself, as a process, and a run that measures it.

This harness used to measure only its own echo loop and said so: no code path
here started the bus (review point 1). That is what this module adds. The real
MCP server — `python -m mattermost_ai_agent_bus`, the entry point
`hosted.main` picks — runs as a subprocess, speaks JSON-RPC over its stdin and
stdout and is pointed at the stub through the proxy, so the faults the echo run
injects are injected into the bus's own sockets.

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
    """
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("MM_", "MATTERMOST_", "CORRAL_", "CLAUDE"))
           and name != "XDG_STATE_HOME"}
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


_interpreter: list[str] | None = None


def interpreter() -> list[str]:
    """The command that runs the bus, found by trying, not by assuming.

    `mcp/pyproject.toml` installs the package editable into `mcp/.venv`, and CI
    has only `uv`. This interpreter — the testrig's — has neither, so each
    candidate is asked whether it can import the package, bounded by a timeout;
    the first that can is used. A run under an interpreter without `mcp` would
    otherwise fail as a child that died with a traceback on stderr, which reads
    like a bus that never started.
    """
    global _interpreter
    if _interpreter is not None:
        return _interpreter
    candidates = [[sys.executable]]
    venv = MCP / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.exists():
        candidates.append([str(venv)])
    if shutil.which("uv"):
        candidates.append(["uv", "run", "--directory", str(MCP), "--no-sync", "python"])
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
    raise RuntimeError("no interpreter here imports mattermost_ai_agent_bus: "
                       "run `uv sync --directory mcp` first")


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
        self._state = tempfile.mkdtemp(prefix="bus-testrig-")
        argv = interpreter() + ["-m", "mattermost_ai_agent_bus"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(MCP), env=_clean_env(self.base_url, self.token, self._state),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=4 << 20)
        except OSError as exc:
            shutil.rmtree(self._state, ignore_errors=True)
            raise RuntimeError(f"the bus did not start: {exc}") from exc
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
        orphan is left for the machine to inherit."""
        proc, self._proc = self._proc, None
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(BrokenPipeError, ConnectionError):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), STOP_SEC)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader, self._stderr):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader = self._stderr = None
        self._pending.clear()
        # The state directory is this run's alone (a throwaway XDG_STATE_HOME);
        # leaving it behind would grow the machine's temp directory by one per
        # server, which a 10-run-per-fault plan multiplies.
        if self._state:
            shutil.rmtree(self._state, ignore_errors=True)
            self._state = ""

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
    run.started = time.monotonic()
    async with contextlib.AsyncExitStack() as stack:
        stub = await stack.enter_async_context(MattermostStub())
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        stub.identify(RECEIVER_TOKEN, RECEIVER_ID, "agent-receiver")
        proxy: GatewayProxy | None = None
        if fault is not None:
            proxy = await stack.enter_async_context(GatewayProxy(stub.url, fault))
        url = proxy.url if proxy is not None else stub.url
        sender = BusProcess(url, token=SENDER_TOKEN)
        receiver = BusProcess(url, token=RECEIVER_TOKEN)
        await stack.enter_async_context(sender)
        await stack.enter_async_context(receiver)
        sent_done: float | None = None
        last_arrival = time.monotonic()
        seq_of: dict[str, int] = {}
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
                since the later of the last send and the last arrival."""
                nonlocal last_arrival
                while True:
                    try:
                        events = await receiver.call("wait_for_events", timeout_sec=0.25)
                    except (McpError, RuntimeError, TimeoutError) as exc:
                        run.closed = run.closed or f"{type(exc).__name__}: {exc}"[:300]
                        return
                    for item in events if isinstance(events, list) else []:
                        post = item.get("post") if isinstance(item, dict) else None
                        seq = seq_of.get(str(post.get("id"))) if isinstance(post, dict) else None
                        if seq is not None:
                            run.arrive(seq)
                            last_arrival = time.monotonic()
                    if sent_done is not None and (
                            time.monotonic() - max(last_arrival, sent_done) > settle):
                        return

            if socket_ok:
                reader = asyncio.create_task(receive())
            for seq in range(count):
                run.send(seq)
                post = await sender.call("post_message", message=f"bus-run {seq}")
                run.sent_frames += 1
                if not isinstance(post, dict) or not post.get("id"):
                    run.closed = f"post_message answered without an id: {post!r}"[:300]
                    break
                created = stub.post_of(str(post["id"]))
                seq_of[str(created["id"])] = seq
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
            if proxy is not None:
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
            if stub.unknown:
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
        return False
    return True


async def _await_socket(stub: MattermostStub, receiver: BusProcess, run: Run, *,
                        timeout: float) -> bool:
    """Wait until the bus's socket is open (it dials on its first wait, so a
    frame pushed before that reaches nobody). False when none opened in time:
    the stub still answers the sender, so the run's numbers are its loss, and
    `closed` says the inbox never connected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stub.conns:
            return True
        await asyncio.sleep(0.01)
    run.closed = run.closed or (
        f"the bus opened no socket within {timeout:.0f} s; "
        f"stderr: {receiver.errors[-3:]}")
    return False
