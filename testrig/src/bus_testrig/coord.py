"""Coordinator hard stops (ADR-014 phase 1, the roles and coordinator row of phase 4).

`coord-run` starts `sessions` MCP servers in one home, waits until one of them
holds the crown, SIGKILLs that one's whole process group (no `bye`, no exit
handler: what a crashed host leaves) and measures how long until another
session holds the crown. Then it starts a fresh session, so the home keeps its
size, and does it again, `stops` times.

**Where the time is read.** The take-over ends when a survivor's server writes
`gained` to its crown log (`crown-<uid>.log` in its state directory, RT-6.6
step 11, `crownlog.py`): that is the moment the bus itself says this session
holds the crown, and it is written the same way on both transports (`nats`
for a home over NATS, RT-6.38; `election`, `slot` or `take` on Mattermost). It
starts at the kill (`BusProcess.hard_stop`, host wall clock). The crown log
writes whole seconds, so each value is good to about one second.

**Mattermost is not yet measured.** `--transport mattermost` runs the same steps
against the stand-in, but the stand-in does not carry the Mattermost election
to a crown (no `gained` line in 240 s with two sessions), so such a run ends
with a note and no take-over. The NATS transport is the one this measures.

**Why not the claim's server time.** On NATS every session claims when it joins
(RT-6.38: the tick claims once the bus is synced), and the crown is the first
claim whose presence is fresh. A survivor's claim is therefore *older* than the
kill, and "its claim time minus the kill" would be negative: the take-over is
not a claim, it is the holder's presence going stale. The run records that moment
too, as `presence_stale_s`: the dead holder's last presence heard by the rig's
admin, plus `PRESENCE_FRESH_SEC` (read from `natsbus.py`), minus the kill. It is
a lower bound of the take-over, before any survivor has noticed.

**What to expect.** A hard-stopped holder sends no `bye`, so on NATS the crown
passes only once its presence is `PRESENCE_FRESH_SEC` (90 s) old, seen on a
survivor's server clock (which moves with the messages it receives, at the
latest each `HEARTBEAT_SEC`, 30 s), and the survivor's mirror and crown log
follow at its next tick (`CB_HEARTBEAT_SEC`, here `heartbeat_sec`, at least
30 s). That is 90 to 150 s, against the phase-4 target of 30 s at p95. The rig
reports it; it does not change the bus.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import natsrig
from .bus import MCP, BusProcess, McpError
from .proxy import ByteProxy
from .runner import Run

#: How long a stop waits for a survivor to hold the crown before it counts the
#: take-over as missing. The NATS take-over is expected at 90-150 s.
TAKEOVER_DEADLINE_SEC = 400.0
#: How long the run waits for the first crown of the home.
FIRST_CROWN_SEC = 240.0
POLL_SEC = 0.5


def presence_fresh_sec() -> float:
    """`PRESENCE_FRESH_SEC` as natsbus.py says today (read, not copied, so it cannot drift)."""
    text = (MCP / "src" / "mattermost_ai_agent_bus" / "natsbus.py").read_text()
    found = re.search(r"^PRESENCE_FRESH_SEC\s*=\s*([0-9.]+)", text, re.MULTILINE)
    return float(found.group(1)) if found else 90.0


def crown_lines(state_dir: str, uid: str) -> list[dict[str, Any]]:
    """The session's crown log, line by line (empty while there is none)."""
    if not state_dir:
        return []
    out: list[dict[str, Any]] = []
    for path in Path(state_dir).rglob(f"crown-{uid}.log"):
        with contextlib.suppress(OSError):
            for line in path.read_text().splitlines():
                with contextlib.suppress(ValueError):
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        out.append(entry)
    return out


def _utc(stamp: str) -> float:
    """The crown log's `t` (`%Y-%m-%dT%H:%M:%SZ`, UTC) as epoch seconds."""
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))


def holds(lines: list[dict[str, Any]]) -> bool:
    """Whether the crown log's last word is `gained`."""
    events = [e for e in lines if e.get("event") in ("gained", "lost")]
    return bool(events) and events[-1].get("event") == "gained"


@dataclass
class Session:
    who: natsrig.Identity
    proc: BusProcess


@dataclass
class Stop:
    """One hard stop: who held the crown, when it was killed, who took over when."""

    killed: str
    killed_at: float
    successor: str = ""
    gained_at: float | None = None
    way: str = ""
    presence_stale_s: float | None = None

    @property
    def takeover_s(self) -> float | None:
        return None if self.gained_at is None else max(0.0, self.gained_at - self.killed_at)

    def row(self) -> dict[str, Any]:
        return {"killed": self.killed, "successor": self.successor or None, "way": self.way or None,
                "takeover_s": None if self.takeover_s is None else round(self.takeover_s, 1),
                "presence_stale_s": None if self.presence_stale_s is None
                else round(self.presence_stale_s, 1)}


@dataclass
class CoordRun(Run):
    stops: list[Stop] = field(default_factory=list)
    transport: str = "nats"
    heartbeat_sec: float = 30.0

    def report(self) -> dict[str, Any]:
        out = super().report()
        out["stops"] = [s.row() for s in self.stops]
        out["heartbeat_sec"] = self.heartbeat_sec
        return out


async def coord_run(transport: str = "nats", stops: int = 3, sessions: int = 3,
                    heartbeat_sec: float = 30.0,
                    deadline_sec: float = TAKEOVER_DEADLINE_SEC) -> CoordRun:
    """`stops` hard stops of the crown holder in a home of `sessions` sessions.

    `heartbeat_sec` is the sessions' `CB_HEARTBEAT_SEC` (the tick that arms the
    crown and writes the crown log; the bus's default is 300 s, its minimum 30 s).
    A take-over that does not come within `deadline_sec` is recorded as missing
    (`takeover_s` null) and counted in `notes`; the run goes on with the next stop.
    """
    if transport not in ("nats", "mattermost"):
        raise ValueError(f"unknown transport {transport!r}")
    if sessions < 2:
        raise ValueError("a take-over needs at least two sessions")
    from .stub import MattermostStub

    run = CoordRun(name="coord-run", count=stops, target=f"bus-{transport}",
                   fault="coordinator-hard-stop",
                   fault_params={"sessions": sessions, "stops": stops,
                                 "deadline_sec": deadline_sec},
                   transport=transport, heartbeat_sec=heartbeat_sec)
    fresh = presence_fresh_sec()
    pool = [natsrig.identity(f"c{i}") for i in range(sessions + stops)]
    run.started = time.monotonic()
    alive: list[Session] = []
    extra = {"CB_HEARTBEAT_SEC": str(int(heartbeat_sec))}
    async with contextlib.AsyncExitStack() as stack:
        stub = await stack.enter_async_context(MattermostStub())
        for who in pool:
            stub.identify(who.token, who.uid, who.username)
        admin: natsrig.Admin | None = None
        nats_url = ""
        if transport == "nats":
            server = natsrig.NatsServer(pool)
            stack.push_async_callback(server.stop)
            await server.start()
            admin = natsrig.Admin(server, pool)
            stack.push_async_callback(admin.close)
            await admin.connect()
            await admin.streams()
            relay = await stack.enter_async_context(
                ByteProxy("127.0.0.1", server.port, on_connect=admin.on_connect))
            nats_url = relay.url

        async def stop_all() -> None:
            for s in alive:
                with contextlib.suppress(Exception):
                    await s.proc.stop()
        stack.push_async_callback(stop_all)

        async def launch(who: natsrig.Identity) -> Session:
            if transport == "nats":
                proc = natsrig.nats_session(stub.url, who, nats_url, extra=extra)
            else:
                proc = BusProcess(stub.url, token=who.token, extra_env=extra)
            await proc.start()
            session = Session(who, proc)
            alive.append(session)
            await proc.call("get_me")
            return session

        try:
            for who in pool[:sessions]:
                await launch(who)
            spare = list(pool[sessions:])
            holder = await _wait_holder(alive, FIRST_CROWN_SEC)
            for _ in range(stops):
                if holder is None:
                    run.notes.append("no session held the crown: the run stopped there")
                    if transport == "mattermost" and not run.stops:
                        # Tried on 2026-11-26: two sessions on the stand-in, 240 s, no
                        # `gained` line. The stub answers the status and roster calls
                        # but does not carry the Mattermost election to a crown, so
                        # the take-over on Mattermost is not yet measured.
                        run.notes.append("the Mattermost stand-in carries no election to a "
                                         "crown: the take-over on Mattermost is not yet measured")
                    break
                stop = Stop(killed=holder.who.label, killed_at=await holder.proc.hard_stop())
                alive.remove(holder)
                run.stops.append(stop)
                if admin is not None:
                    last = admin.last_presence.get(holder.who.name)
                    if last is not None:
                        stop.presence_stale_s = last + fresh - stop.killed_at
                successor = await _wait_gained(alive, stop, deadline_sec)
                run.sent_frames += 1
                if successor is None:
                    run.notes.append(f"stop {len(run.stops)}: no survivor held the crown within "
                                     f"{deadline_sec:.0f} s of the kill")
                else:
                    run.takeovers.append(stop.takeover_s or 0.0)
                if spare:
                    await launch(spare.pop(0))
                holder = successor or await _wait_holder(alive, FIRST_CROWN_SEC)
        except (McpError, RuntimeError, TimeoutError, OSError) as exc:
            run.closed = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            run.ended = time.monotonic()
            if stub.unknown:
                run.unknown = list(stub.unknown)
                run.notes.append(f"the stub did not know {len(stub.unknown)} call(s): "
                                 f"{stub.unknown[:5]}")
            tails = [line for s in alive for line in s.proc.errors]
            run.tail = natsrig._nats_tail(tails)
    return run


async def _wait_holder(alive: list[Session], timeout: float) -> Session | None:
    """The session whose crown log says it holds the crown, once one does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for s in alive:
            if holds(crown_lines(s.proc.state_dir, s.who.uid)):
                return s
        await asyncio.sleep(POLL_SEC)
    return None


async def _wait_gained(alive: list[Session], stop: Stop, timeout: float) -> Session | None:
    """The first survivor whose crown log gains the crown at or after the kill (to
    the log's second), with the time it wrote; None when none does in time."""
    deadline = time.monotonic() + timeout
    floor = int(stop.killed_at)
    while time.monotonic() < deadline:
        for s in alive:
            for entry in crown_lines(s.proc.state_dir, s.who.uid):
                if entry.get("event") != "gained" or not isinstance(entry.get("t"), str):
                    continue
                at = _utc(entry["t"])
                if at >= floor:
                    stop.successor, stop.gained_at = s.who.label, at
                    stop.way = str(entry.get("way") or "")
                    return s
        await asyncio.sleep(POLL_SEC)
    return None
