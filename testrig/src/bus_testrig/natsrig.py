"""The bus over NATS (ADR-014 phase 1): a local nats-server, the login service's part, the run.

`bus-run --transport nats` measures the same path `bus.bus_run` measures — one
real MCP server posts with `post_message`, another receives with
`wait_for_events` — for sessions whose home runs over NATS (`CB_NATS_HOMES`,
RT-6.36). What stands in for what:

* **nats-server** is the real binary (`CB_NATS_SERVER` or `MM_NATS_SERVER`, else
  `nats-server` on `PATH`; version 2.15.3 as in CI). Without it a run says so
  and does not start (`Unavailable`).
* **The login service** is two things here. Its credential is a static nkey
  user per agent in the server's config, with the rights `callout/perms.go`
  grants for the home channel (`user_entry`). Its consumers and streams are
  made by an admin user (`Admin`), as `consumers/manager.go` makes them.
* **The login** is `natsserver.FakeLogin`, installed by the shim the sessions
  run (`python -m bus_testrig.natsserver`).
* **Mattermost** is still the stub: a NATS home's session uses Mattermost for
  its bot, roster and status (the half that is not the home's traffic).

The config and the consumers are a COPY of the `rig` fixture and of
`_consumer_specs`/`_login_service` in `mcp/tests/test_natsbus.py` (around lines
940-1078, "against a real nats-server"), which in turn copy the Go services.
When either changes, change this too: a rig that grants other rights than the
login service measures another bus. A drift shows as a refused publish in the
sessions' stderr (the run's `stderr_tail`) and a loss of everything.

The faults on NATS (`NATS_FAULTS`), each a window opened half way through the
sends and lasting `fault_sec`:

  restart      nats-server SIGKILLed, started again on the same port and store
  hang         nats-server SIGSTOPped, SIGCONTed at the window's end (`proc.pause`)
  kill-client  the receiving MCP server SIGKILLed, started again on the same
               identity and state directory (a resumed session)
  cut, blackhole, stall, halfopen
               the byte proxy in front of NATS's port (`proxy.ByteProxy`)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proc
from .bus import MCP, REPO, BusProcess, McpError, _arrivals, _open_inbox
from .proxy import BYTE_KINDS, NATS_FAULTS, ByteProxy
from .runner import Run

#: The home every session of a run shares: the stub's only channel.
HOME = "agents"
OWNER = "testrig"
ADMIN = "admin"
START_SEC = 45.0
SERVER_START_SEC = 10.0

# --- the login service's part, copied from mcp/tests/test_natsbus.py ---------------------

# The streams exactly as services/auth/internal/consumers/streams.go BusStreamJSON("bus", 0) and
# PresenceStreamJSON("presence", 0) make them (copied from test_natsbus.py, BUS_STREAM/PRESENCE_STREAM).
_STREAM_COMMON = ('"retention":"limits","storage":"file","discard":"old",%s,"num_replicas":1,"no_ack":true,'
                  '"duplicate_window":100000000,"allow_direct":false,"mirror_direct":false,"deny_delete":true,'
                  '"deny_purge":true,"allow_rollup_hdrs":false,"allow_msg_ttl":false,"allow_msg_counter":false,'
                  '"allow_atomic":false,"allow_msg_schedules":false,"allow_batched":false')
BUS_STREAM = ('{"name":"BUS","subjects":["bus.>"],' + _STREAM_COMMON %
              '"max_age":1209600000000000,"max_bytes":1073741824,"max_msg_size":65536' + "}")
PRESENCE_STREAM = ('{"name":"PRESENCE","subjects":["presence.>"],' + _STREAM_COMMON %
                   '"max_age":86400000000000,"max_msgs_per_subject":1,"max_bytes":16777216,"max_msg_size":4096' + "}")
# StatsStreamJSON("stats", 0): 30 days, 500 lines per agent, 64 MiB, 8 KiB per line (RT-6.43).
# test_natsbus.py makes no STATS stream; the rig does, because its sessions run their
# whole server and publish their STATS line.
STATS_STREAM = ('{"name":"STATS","subjects":["stats.>"],' + _STREAM_COMMON %
                '"max_age":2592000000000000,"max_msgs_per_subject":500,"max_bytes":67108864,"max_msg_size":8192' + "}")
HOUR_NS = 3_600_000_000_000
# The keys of the hello's `since` that are no channel (consumers/manager.go SinceDM, SincePresence).
SINCE_DM = "@dm"
SINCE_PRESENCE = "@presence"


def consumer_specs(agent: str, channels: list[str],
                   since: dict[str, int] | None = None) -> list[tuple[str, str, dict[str, Any]]]:
    """Consumers like consumers/manager.go desired() and config(): (stream, name, config).
    A copy of test_natsbus.py `_consumer_specs`."""
    since = since or {}
    out: list[tuple[str, str, dict[str, Any]]] = []
    filters = ["presence.*.*"]  # every agent's presence: <owner>.<name> (RT-6.37)

    def cfg(name: str, deliver: str, flt: list[str], policy: str = "new") -> dict[str, Any]:
        c: dict[str, Any] = {"durable_name": name, "deliver_subject": deliver, "ack_policy": "none",
                             "deliver_policy": policy, "inactive_threshold": HOUR_NS,
                             "metadata": {"agent": agent}}
        if len(flt) == 1:
            c["filter_subject"] = flt[0]
        else:
            c["filter_subjects"] = flt
        return c

    for ch in channels:
        c = cfg(f"{agent}_ch_{ch}", f"deliver.{agent}.ch.{ch}", [f"bus.ch.{ch}.*"])
        if since.get(ch):  # what the login service does with `since`
            c["deliver_policy"], c["opt_start_seq"] = "by_start_sequence", since[ch] + 1
        out.append(("BUS", c["durable_name"], c))
        filters += [f"presence.crown.{ch}.*", f"presence.member.{ch}.*"]
    dm = cfg(f"{agent}_dm", f"deliver.{agent}.dm", [f"bus.dm.{agent}.*"])
    if since.get(SINCE_DM):  # the same as the channels: restart after the last DM seen
        dm["deliver_policy"], dm["opt_start_seq"] = "by_start_sequence", since[SINCE_DM] + 1
    out.append(("BUS", dm["durable_name"], dm))
    pr = cfg(f"{agent}_presence", f"deliver.{agent}.presence", filters, "last_per_subject")
    out.append(("PRESENCE", pr["durable_name"], pr))
    return out


def agent_name(owner: str, public_key: str, prefix: str = "agent-") -> str:
    """The agent's NATS name, as `natsbus.agent_name` computes it for an unshared key
    (and Go `name()` in services/auth/internal/callout/service.go): SHA-256 over
    owner, NUL, host nkey; the first 16 bytes as lower-case base32 without padding.
    A copy: the rig needs the name for the config before any session runs. A drift
    makes every session fail with `NameMismatch` or a refused publish."""
    digest = hashlib.sha256(owner.encode() + b"\x00" + public_key.encode()).digest()[:16]
    return prefix + base64.b32encode(digest).decode().lower().rstrip("=")


def user_entry(public_key: str, name: str, channel: str = HOME, owner: str = OWNER) -> str:
    """The static user standing in for the credential services/auth/internal/callout/perms.go
    `permissions` grants for one home channel. The `rig` fixture of test_natsbus.py writes
    the same entry without the ask record and the STATS line: its buses publish neither,
    a whole server does both (RT-6.43), so the rig follows perms.go here."""
    allow = [f"bus.ch.{channel}.{name}", f"presence.crown.{channel}.{name}",
             f"presence.ask.{channel}.{name}", f"presence.member.{channel}.{name}",
             f"bus.dm.*.{name}", f"presence.{owner}.{name}", f"stats.{name}"]
    quoted = ", ".join(f'"{subject}"' for subject in allow)
    return (f'{{nkey: "{public_key}", permissions: {{publish: {{allow: [{quoted}]}}, '
            f'subscribe: {{allow: ["deliver.{name}.>"]}}}}}}')


# --- identities -------------------------------------------------------------------------

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class Identity:
    """One agent of a run: its Mattermost bot (token, user id, username, for the stub)
    and its NATS key and name."""

    label: str
    token: str
    uid: str
    username: str
    seed: str = field(repr=False)
    public_key: str = ""
    name: str = ""


def identity(label: str, owner: str = OWNER) -> Identity:
    """A fresh agent: a new nkey (as FakeLogin makes one) and a well-formed Mattermost id."""
    import nkeys

    seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER).decode()
    public = bytes(nkeys.from_seed(seed.encode()).public_key).decode()
    uid = "".join(secrets.choice(_ID_ALPHABET) for _ in range(26))
    return Identity(label=label, token=f"testrig-{label}-{uid[:6]}", uid=uid,
                    username=f"agent-{label}", seed=seed, public_key=public,
                    name=agent_name(owner, public))


def session_env(who: Identity, nats_url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """What a NATS-home session's shim reads, on top of `bus._clean_env`."""
    env = {
        "TESTRIG_NKEY_SEED": who.seed,
        "TESTRIG_OWNER": OWNER,
        "CB_NATS_HOMES": HOME,
        "CB_NATS_URL": nats_url,
        "CB_LOGIN_ISSUER": "http://127.0.0.1:9/realms/testrig",
        "CB_LOGIN_CLIENT_ID": "testrig",
        "PYTHONPATH": os.pathsep.join([str(MCP / "src"), str(REPO / "testrig" / "src")]),
    }
    env.update(extra or {})
    return env


def nats_session(base_url: str, who: Identity, nats_url: str, *, state_dir: str | None = None,
                 extra: dict[str, str] | None = None) -> BusProcess:
    """A BusProcess that runs the shim for `who`, a session whose home is over NATS."""
    return BusProcess(base_url, token=who.token, module="bus_testrig.natsserver",
                      extra_env=session_env(who, nats_url, extra), state_dir=state_dir)


# --- the server -------------------------------------------------------------------------


class Unavailable(RuntimeError):
    """No nats-server binary here: the NATS transport cannot run, and says why."""


def server_binary() -> str | None:
    """The nats-server the rig runs: CB_NATS_SERVER, MM_NATS_SERVER (the old name,
    as mcp/tests/test_natsbus.py reads it), else `nats-server` on PATH."""
    for name in ("CB_NATS_SERVER", "MM_NATS_SERVER"):
        value = os.environ.get(name, "").strip()
        if value and Path(value).exists():
            return value
    return shutil.which("nats-server")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NatsServer:
    """One nats-server with JetStream, the agents' static users and an admin user.

    `restart` SIGKILLs it and starts it again on the same port and store
    directory: the streams and consumers survive (file storage), as on a host
    whose server crashed."""

    def __init__(self, agents: list[Identity], workdir: Path | None = None,
                 binary: str | None = None) -> None:
        binary = binary or server_binary()
        if not binary:
            raise Unavailable("no nats-server here: set CB_NATS_SERVER=<path to nats-server 2.15.3>")
        self.binary = binary
        self._own_dir = workdir is None
        self.dir = Path(tempfile.mkdtemp(prefix="bus-testrig-nats-")) if workdir is None else workdir
        self.port = _free_port()
        self.admin_password = secrets.token_hex(8)
        self.conf = self.dir / "nats.conf"
        users = [f'{{user: {ADMIN}, password: "{self.admin_password}"}}']
        users += [user_entry(a.public_key, a.name) for a in agents]
        self.conf.write_text(f'port: {self.port}\nhost: 127.0.0.1\n'
                             f'jetstream {{ store_dir: "{self.dir / "js"}" }}\n'
                             f'accounts {{ BUS {{ jetstream: enabled\n users: [{", ".join(users)}] }} }}\n')
        self.proc: subprocess.Popen[bytes] | None = None
        self.restarts = 0

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    async def start(self) -> None:
        self.proc = subprocess.Popen([self.binary, "-c", str(self.conf)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + SERVER_START_SEC
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"nats-server exited with {self.proc.returncode}")
            with contextlib.suppress(OSError):
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"nats-server did not listen within {SERVER_START_SEC:.0f} s")

    def kill(self) -> None:
        if self.proc is not None:
            proc.resume(self.proc.pid)  # a hung server must not survive its kill
            proc.kill_child(self.proc)

    async def restart(self, down_sec: float) -> None:
        """SIGKILL, `down_sec` with no server, then the same config again."""
        self.kill()
        await asyncio.sleep(down_sec)
        await self.start()
        self.restarts += 1

    async def hang(self, seconds: float) -> None:
        pid = self.pid
        if pid is None:
            raise RuntimeError("nats-server is not running")
        proc.pause(pid)
        try:
            await asyncio.sleep(seconds)
        finally:
            proc.resume(pid)

    async def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            proc.resume(self.proc.pid)
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 10)
            except subprocess.TimeoutExpired:
                proc.kill_child(self.proc)
        if self._own_dir:
            shutil.rmtree(self.dir, ignore_errors=True)


class Admin:
    """The login service's JetStream work, as the admin user: the streams once, the
    consumers per agent (`_login_service` of test_natsbus.py, without `since`), and
    a watch on the presence subjects that notes when each agent was last heard."""

    def __init__(self, server: NatsServer, agents: list[Identity] | None = None) -> None:
        self.server = server
        self.nc: Any = None
        #: nkey public key -> agent name, for `on_connect`
        self.names = {a.public_key: a.name for a in agents or []}
        self.logins = 0
        #: agent name -> host time (time.time) its last presence message was heard
        self.last_presence: dict[str, float] = {}
        #: (agent name, host time) of each crown claim heard
        self.claims: list[tuple[str, float]] = []

    async def connect(self) -> None:
        from nats.aio.client import Client as NatsClient

        self.nc = NatsClient()
        async def quiet(_exc: Exception) -> None:
            return None  # a restarted server is the run's fault, not an error of the admin

        await self.nc.connect(self.server.url, user=ADMIN, password=self.server.admin_password,
                              max_reconnect_attempts=-1, reconnect_time_wait=0.2,
                              connect_timeout=2, error_cb=quiet)
        await self.nc.subscribe("presence.>", cb=self._on_presence)

    async def _on_presence(self, msg: Any) -> None:
        parts = msg.subject.split(".")
        now = time.time()
        if len(parts) == 4 and parts[1] == "crown":
            self.claims.append((parts[3], now))
        if len(parts) == 3 and parts[1] == OWNER:  # presence.<owner>.<name>: the heartbeat
            self.last_presence[parts[2]] = now

    async def api(self, subject: str, body: dict[str, Any] | str | None = None) -> dict[str, Any]:
        data = body if isinstance(body, str) else json.dumps(body or {})
        r = await self.nc.request(subject, data.encode(), timeout=5)
        out: dict[str, Any] = json.loads(r.data)
        if "error" in out:
            raise RuntimeError(f"{subject}: {out['error']}")
        return out

    async def consumer_info(self, name: str) -> dict[str, Any]:
        """The BUS consumer as the server holds it (`kill-client`'s evidence):
        the delivered/consumer sequence and the pending count at this moment.
        A failure is reported in place, never raised into the run."""
        try:
            out = await self.api(f"$JS.API.CONSUMER.INFO.BUS.{name}")
        except (RuntimeError, OSError, TimeoutError) as exc:
            return {"error": str(exc)[:120]}
        return {"delivered_stream_seq": out.get("delivered", {}).get("stream_seq"),
                "delivered_consumer_seq": out.get("delivered", {}).get("consumer_seq"),
                "ack_floor_stream_seq": out.get("ack_floor", {}).get("stream_seq"),
                "num_pending": out.get("num_pending")}

    async def streams(self) -> None:
        for name, cfg in (("BUS", BUS_STREAM), ("PRESENCE", PRESENCE_STREAM), ("STATS", STATS_STREAM)):
            await self.api(f"$JS.API.STREAM.CREATE.{name}", cfg)

    async def consumers(self, agent: str, channels: list[str] | None = None,
                        since: dict[str, int] | None = None) -> None:
        """What the login service does at each connect (test_natsbus.py `_login_service`,
        consumers/manager.go Reconcile): the consumers made, a channel's or the DM's
        made anew from `since` when the hello names a position, the presence consumer
        made anew when it says `@presence` (a new session reads presence afresh)."""
        since = since or {}
        for stream, name, cfg in consumer_specs(agent, channels or [HOME], since):
            fresh = (stream == "BUS" and "_ch_" in name and since.get(name.split("_ch_", 1)[1])) \
                or (name == f"{agent}_dm" and since.get(SINCE_DM)) \
                or (stream == "PRESENCE" and since.get(SINCE_PRESENCE))
            if fresh:
                r = await self.nc.request(f"$JS.API.CONSUMER.DELETE.{stream}.{name}", b"", timeout=5)
                code = json.loads(r.data).get("error", {}).get("err_code")
                if code not in (None, 10014):  # 10014: no such consumer, made below
                    raise RuntimeError(f"consumer {name} not deleted: {code}")
            await self.api(f"$JS.API.CONSUMER.CREATE.{stream}.{name}",
                           {"stream_name": stream, "config": cfg})

    async def on_connect(self, connect: bytes) -> None:
        """The auth callout's part: a client's CONNECT, read by the byte proxy before it
        reaches the server. Its `nkey` names the agent, its `auth_token` is the bus's
        hello (natsbus.build_hello) with `since`; the consumers are reconciled from it
        before the server sees the CONNECT, as the callout does before it answers."""
        try:
            fields = json.loads(connect)
            hello = json.loads(fields.get("auth_token") or "{}")
        except (ValueError, TypeError):
            return
        agent = self.names.get(str(fields.get("nkey") or ""))
        if agent is None or not isinstance(hello, dict):
            return
        since = {str(k): int(v) for k, v in (hello.get("since") or {}).items()
                 if isinstance(v, int)}
        self.logins += 1
        await self.consumers(agent, [str(hello.get("home") or HOME)], since)

    async def wait_heard(self, names: list[str], timeout: float) -> list[str]:
        """Wait until each agent's presence was heard; returns the ones that were not."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            missing = [n for n in names if n not in self.last_presence]
            if not missing:
                return []
            await asyncio.sleep(0.1)
        return [n for n in names if n not in self.last_presence]

    async def close(self) -> None:
        if self.nc is not None:
            with contextlib.suppress(Exception):
                await self.nc.close()


# --- the delivery run -------------------------------------------------------------------


@dataclass
class _Receiver:
    """The receiving session, replaceable: `kill-client` swaps it for a new process."""

    proc: BusProcess
    ready: asyncio.Event = field(default_factory=asyncio.Event)


async def nats_bus_run(count: int = 10, fault: str | None = None, *, fault_sec: float = 2.0,
                       stall_sec: float = 0.5, send_interval: float = 0.05,
                       settle_sec: float | None = None) -> Run:
    """`bus.bus_run` for a home over NATS: `count` posts from one session to another,
    with `fault` (one of NATS_FAULTS, or None for the baseline) opened half way
    through the sends for `fault_sec`.

    A send that fails is not counted as sent (as in `bus.bus_run`), but the run
    goes on: under a fault the next send may well pass, and stopping at the first
    failure would measure the fault's start, not the bus. The failed sends are in
    `notes`. The reader ends once sending is done and nothing arrived for
    `settle_sec` (default: 1 s, or `fault_sec` + 10 s under a fault, so a session
    that reconnects with back-off still delivers inside the window).
    """
    if fault is not None and fault not in NATS_FAULTS:
        raise ValueError(f"unknown NATS fault {fault!r}; one of {NATS_FAULTS}")
    from .stub import MattermostStub

    settle = settle_sec if settle_sec is not None else (fault_sec + 10.0 if fault else 1.0)
    run = Run(name="bus-run", count=count, target="bus-nats", fault=fault or "none",
              fault_params=({"window_sec": fault_sec, "at_send": count // 2}
                            | ({"stall_sec": stall_sec} if fault == "stall" else {}))
              if fault else {}, send_interval=send_interval)
    sender_id, receiver_id = identity("sender"), identity("receiver")
    run.started = time.monotonic()
    failed_sends = 0
    async with contextlib.AsyncExitStack() as stack:
        server = NatsServer([sender_id, receiver_id])
        stack.push_async_callback(server.stop)
        await server.start()
        admin = Admin(server, [sender_id, receiver_id])
        stack.push_async_callback(admin.close)
        await admin.connect()
        await admin.streams()
        stub = await stack.enter_async_context(MattermostStub())
        for who in (sender_id, receiver_id):
            stub.identify(who.token, who.uid, who.username)
        relay = await stack.enter_async_context(
            ByteProxy("127.0.0.1", server.port, stall_sec=stall_sec, on_connect=admin.on_connect))
        receiver_state = tempfile.mkdtemp(prefix="bus-testrig-resume-")
        stack.callback(shutil.rmtree, receiver_state, True)
        sender = nats_session(stub.url, sender_id, relay.url)
        await stack.enter_async_context(sender)
        rx = _Receiver(nats_session(stub.url, receiver_id, relay.url, state_dir=receiver_state))
        await rx.proc.start()
        # The receiver's BUS consumer: its delivered position and pending count
        # are what a lost sequence is read beside (issue #213).
        consumer = f"{receiver_id.name}_ch_{HOME}"

        async def stop_receiver() -> None:
            await rx.proc.stop()
        stack.push_async_callback(stop_receiver)

        seq_of: dict[str, int] = {}
        seq_of_text: dict[str, int] = {}
        sent_done: float | None = None
        last_arrival = time.monotonic()
        reader: asyncio.Task[None] | None = None
        window: asyncio.Task[None] | None = None
        try:
            await sender.call("get_me")
            await rx.proc.call("get_me")
            missing = await admin.wait_heard([sender_id.name, receiver_id.name], START_SEC)
            if missing:
                run.unmeasured = f"the NATS bus of {len(missing)} session(s) never started"
                run.closed = f"no presence from {missing} within {START_SEC:.0f} s"
                return run
            if not await _open_inbox(rx.proc, run):
                return run
            rx.ready.set()

            async def receive() -> None:
                """`bus.bus_run`'s reader, waiting while the receiver is replaced."""
                nonlocal last_arrival
                failures = 0
                while True:
                    await rx.ready.wait()
                    try:
                        events = await rx.proc.call("wait_for_events", timeout_sec=0.25)
                    except (McpError, RuntimeError, TimeoutError) as exc:
                        run.closed = run.closed or f"{type(exc).__name__}: {exc}"[:300]
                        failures += 1
                        if failures > 40 and rx.ready.is_set():
                            return
                        await asyncio.sleep(0.25)
                        continue
                    failures = 0
                    for seq in _arrivals(events, seq_of, seq_of_text):
                        run.arrive(seq)
                        last_arrival = time.monotonic()
                    if sent_done is not None and window is not None and not window.done():
                        continue  # never end while the fault's window is open
                    if sent_done is not None and (
                            time.monotonic() - max(last_arrival, sent_done) > settle):
                        return

            async def open_window() -> None:
                assert fault is not None
                if fault in BYTE_KINDS:
                    await relay.inject(fault, fault_sec)
                elif fault == "restart":
                    await server.restart(fault_sec)
                elif fault == "hang":
                    await server.hang(fault_sec)
                elif fault == "kill-client":
                    rx.ready.clear()
                    await rx.proc.hard_stop()
                    run.consumer_at_kill = await admin.consumer_info(consumer)
                    await asyncio.sleep(fault_sec)
                    rx.proc = nats_session(stub.url, receiver_id, relay.url, state_dir=receiver_state)
                    await rx.proc.start()
                    await asyncio.sleep(0.5)  # its first CONNECT reconciles the consumer
                    run.consumer_at_resume = await admin.consumer_info(consumer)
                    # The resumed session's inbox opens on its first wait, which is the
                    # reader's own: a wait made here would hand over the posts that
                    # waited for the session and nobody would count them.
                    rx.ready.set()

            reader = asyncio.create_task(receive())
            for seq in range(count):
                if fault is not None and seq == count // 2 and window is None:
                    window = asyncio.create_task(open_window())
                    await asyncio.sleep(0)  # let the window open before this send
                text = f"bus-run {seq}"
                seq_of_text[text] = seq
                run.send(seq)
                post: Any = None
                try:
                    post = await sender.call("post_message", message=text)
                except (McpError, RuntimeError, TimeoutError, OSError) as exc:
                    run.closed = run.closed or f"post_message failed: {type(exc).__name__}: {exc}"[:300]
                if not isinstance(post, dict) or not post.get("id"):
                    run.sent.pop()
                    # Its text stays known: a send that failed may still arrive, and
                    # the report counts it as `failed_but_arrived` rather than losing
                    # the evidence (review round 2).
                    failed_sends += 1
                else:
                    run.sent_frames += 1
                    seq_of[str(post["id"])] = seq
                await asyncio.sleep(send_interval)
            if window is not None:
                await window
            sent_done = time.monotonic()
            await reader
        except (McpError, RuntimeError, TimeoutError, OSError) as exc:
            if not run.closed:
                run.closed = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            for task in (reader, window):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            run.ended = time.monotonic()
            run.relay = relay.counters() | {"server_restarts": server.restarts}
            if relay.failed:
                run.notes.append(relay.failed)
            if failed_sends:
                run.notes.append(f"{failed_sends} post_message call(s) failed under the fault "
                                 "and are not counted as sent")
            if stub.posts:
                # The home's traffic belongs on NATS: a post in the stub went over
                # Mattermost, so this run did not measure the transport it names.
                run.notes.append(f"the Mattermost stub holds {len(stub.posts)} post(s): the home's "
                                 "traffic did not stay on NATS")
            if stub.unknown:
                run.unknown = list(stub.unknown)
                run.notes.append(f"the stub did not know {len(stub.unknown)} call(s): "
                                 f"{stub.unknown[:5]}")
            run.tail = _nats_tail(rx.proc.errors or sender.errors)
        return run


_SECRETISH = re.compile(r"(seed|token|password)\S*", re.IGNORECASE)


def _nats_tail(lines: list[str]) -> list[str]:
    """The last error lines, without anything that looks like a key or token."""
    return [_SECRETISH.sub(r"\1=…", line) for line in lines[-15:]]

