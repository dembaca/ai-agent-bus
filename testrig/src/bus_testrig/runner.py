"""Runs: send frames, fault the channel, measure what arrived.

Two runs share this bookkeeping. `echo_run` measures the harness's own echo
loop — a real `websockets` server, the real proxy in front of it and the
harness's own client — and proves the harness works where the bus does not
run. `bus.bus_run` measures the bus itself: two real MCP server processes, one
posting and one receiving, through the stub.

The measurement is independent of when a test reads: `Sent` and `Received`
are recorded at the moment each side acts, so a slow reader cannot inflate a
latency and a stall cannot be mistaken for a loss.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

import websockets

from . import measurands
from .proxy import FrameProxy, ProxyFault


@dataclass
class Sent:
    seq: int
    at: float


@dataclass
class Received:
    seq: int
    at: float
    copies: int = 1


def _pct(ordered: list[float], p: float) -> float:
    """The p-th percentile of an already ordered list, nearest rank.

    `ceil(p/100 * n) - 1` (review point 6): n = 1 gives the only value, and
    n = 100 at p = 95 gives index 94, not 95. The old `round(p/100*n + 0.5)-1`
    was one off for most n.
    """
    if not ordered:
        raise ValueError("empty list")
    rank = math.ceil(p / 100.0 * len(ordered)) - 1
    return ordered[max(0, min(rank, len(ordered) - 1))]


@dataclass
class Run:
    """One fault run's bookkeeping and its measurements."""

    name: str
    count: int = 100
    sent: list[Sent] = field(default_factory=list)
    received: dict[int, Received] = field(default_factory=dict)
    started: float = 0.0
    ended: float = 0.0
    target: str = "echo"
    fault: str = "none"
    fault_params: dict[str, Any] = field(default_factory=dict)
    send_interval: float = 0.0
    commit: str = ""
    closed: str = ""  # set when a fault ended the connection (a cut)
    #: how many frames the run had sent when it stopped sending
    sent_frames: int = 0
    #: the bus's own stderr when the target was a process: a failure is read
    #: with what the server said, not guessed from a missing frame
    tail: list[str] = field(default_factory=list)
    #: what the run saw that is not a delivery result (a stub call it could not
    #: answer), so the report says where its numbers came from
    notes: list[str] = field(default_factory=list)
    #: set when the run never observed the path under test at all (the inbox
    #: never opened): `loss` then says "everything", not "the channel lost
    #: this", and a caller must not compare it (review point 2)
    unmeasured: str = ""
    #: the proxy's own counters when a fault sat in front of the socket
    #: (received, forwarded, altered): the frames it saw, passed and changed
    relay: dict[str, int] = field(default_factory=dict)
    #: calls the stand-in could not answer, HTTP or WebSocket. Not in the
    #: report's JSON (it stays what it was) but on the run, because the exit
    #: code is decided from it (review point 2).
    unknown: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def send(self, seq: int) -> Sent:
        item = Sent(seq=seq, at=time.monotonic())
        self.sent.append(item)
        return item

    def arrive(self, seq: int, at: float | None = None) -> None:
        hit = self.received.get(seq)
        if hit is None:
            self.received[seq] = Received(seq=seq, at=at if at is not None else time.monotonic())
        else:
            hit.copies += 1

    def _values(self) -> dict[str, float | None]:
        """The formulas, by measurand `id` (review point 8: never by name).

        A measurand whose data this run did not collect is `None`, never 0 —
        a zero would read as "nothing was lost" when nothing was measured.
        """
        by_seq = {s.seq: s for s in self.sent}
        open_latencies = sorted((r.at - by_seq[seq].at) * 1000.0
                                for seq, r in self.received.items() if seq in by_seq)
        # A sent frame that never arrived is lost, whatever ended the
        # connection: the acceptance compares exactly this number. The report
        # also names `connection_closed`, so a cut is not mistaken for a
        # delivery fault of the channel under test (re-point 2 of the third
        # re-check: nulling it here hid 7 of 10).
        lost = sum(1 for seq in by_seq if seq not in self.received)
        dupes = sum(r.copies - 1 for r in self.received.values())
        return {
            "loss": lost,
            "latency_p50": statistics.median(open_latencies) if open_latencies else None,
            "latency_p95": _pct(open_latencies, 95) if open_latencies else None,
            "duplicates": dupes,
        }

    def measure(self, path: Any = None) -> dict[str, dict[str, Any]]:
        """Every measurand in the file, by `id`, measured or named as not.

        Raises when the file declares a `measured` id this run has no formula
        for: that is a broken harness, not a zero."""
        values = self._values()
        out: dict[str, dict[str, Any]] = {}
        for m in (measurands.load(path) if path is not None else measurands.load()):
            if not m.measurable:
                out[m.id] = {"name": m.name, "value": None, "unit": m.unit,
                             "status": m.status, "why": m.why}
                continue
            if m.id not in values:
                raise RuntimeError(f"{m.id} is 'measured' in the file, but no formula computes it")
            value = values[m.id]
            out[m.id] = {
                "name": m.name,
                "value": None if value is None else round(value, 1),
                "unit": m.unit,
                "status": m.status,
            }
        return out

    def report(self) -> dict[str, Any]:
        """What `bus-testrig` prints (review point 15): the run's context with
        its numbers, so a number is never read without what produced it."""
        return {
            "run": self.name,
            "target": self.target,
            "fault": self.fault,
            "fault_params": self.fault_params,
            "send_interval_s": self.send_interval,
            "count": self.count,
            "sent_frames": self.sent_frames or len(self.sent),
            "commit": self.commit,
            "duration_s": round(self.ended - self.started, 3) if self.ended else None,
            "connection_closed": self.closed or None,
            "stderr_tail": self.tail,
            "notes": self.notes,
            "relay": self.relay,
            "measured": self.measure(),
        }


# The faults whose silence is their own doing (issue #47): `drop` discards every
# frame it sees, `cut` is a dead transport the bus recovers by backfill,
# `blackhole` and `halfopen` stop delivery on purpose. A run under one of them
# that delivers nothing is a measurement of that fault - and has to say so,
# because a path that carried nothing reads exactly the same. Every other fault
# passes frames on (`stall` delays, `reorder` swaps, `duplicate` repeats), and
# `none` is the baseline: silence under those is the path.
SILENCING_FAULTS = ("drop", "cut", "blackhole", "halfopen")


def silence(run: Run) -> tuple[bool, str]:
    """`(explained, why)`: frames were sent and **none** arrived (issue #47).

    `explained` is true when the run's own evidence accounts for every frame: a
    fault that silences the line by design, with the relay's counters showing it
    saw them (`drop`, which is meant to discard). False means the path carried
    nothing - about the rig, not about the fault - and `why` names the part.

    No verdict (`True, ""`) when something arrived: there is nothing to explain.
    Counting *delivered* frames alone cannot separate the two, which is why the
    relay's own counters decide the `drop` case: a frame the relay never saw was
    lost before the injection point (the review of #35).
    """
    if not run.sent_frames or run.received or run.unmeasured:
        # `unmeasured` has its own words: the inbox never opened, so nothing
        # arriving says nothing this run did not already say (review of #54).
        return True, ""
    altered = (run.relay or {}).get("altered", 0)
    seen = f" ({altered} of {run.sent_frames} frame(s) altered at the relay)" if run.relay else ""
    if run.fault in SILENCING_FAULTS:
        if run.fault == "drop" and run.relay and altered < run.sent_frames:
            return False, (f"the relay dropped {altered} of {run.sent_frames} frame(s); the "
                           "rest never reached the injection point, so nothing arriving is "
                           "not the fault's doing")
        what = "drops every frame" if run.fault == "drop" else "silences the line"
        return True, (f"nothing arrived: --fault {run.fault} {what}{seen}, so the run "
                      "measures the fault, not delivery")
    return False, (f"--fault {run.fault} lets traffic through, but none of "
                   f"{run.sent_frames} frame(s) arrived: the path carried nothing, so "
                   "this is not a measurement of the fault")


async def echo_run(count: int = 20, fault: ProxyFault | None = None,
                   send_interval: float = 0.0) -> Run:
    """The whole flow with no Lab and no server: an echo endpoint the harness
    starts, the proxy in front of it, and the harness's own client.

    The reader runs as its own task **before** the first send (review point 7):
    a sender that also reads pays for its own queue, which made 20 frames look
    like 1.8 ms and 2000 like 62 ms. `send_interval` paces the sender.

    A frame counts as arrived when its seq comes back; the reader stops when
    every seq arrived or `deadline` passed, so a stall is not a loss (point 5).
    """
    async def echo(conn: Any) -> None:
        # A fault closes this connection on purpose; that is not an error of
        # the echo endpoint, and a traceback on stderr would bury the report.
        try:
            async for frame in conn:
                await conn.send(frame)
        except websockets.ConnectionClosed:
            return

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    run = Run(name="echo-run", count=count, target="echo",
              fault=fault.kind if fault else "none",
              fault_params={"every": fault.every, "after": fault.after,
                            "stall_sec": fault.stall_sec,
                            "reorder_max_wait": fault.reorder_max_wait} if fault else {},
              send_interval=send_interval)
    run.started = time.monotonic()
    # The settle window must outlast every silence the fault can produce, and
    # it starts only when the sender is done: a window that runs during sending
    # reports frames still in flight as losses (re-point 2 in the re-check).
    stall = fault.stall_sec if fault is not None and fault.kind == "stall" else 0.0
    reorder_wait = fault.reorder_max_wait if fault is not None else 0.0
    settle = max(stall, reorder_wait, send_interval) + 0.3
    sent_all = asyncio.Event()
    try:
        # `proxy=None` on both connections (follow-up review point 4): the
        # endpoints are loopback and `websockets` would otherwise use the
        # environment's proxy, so a machine with `HTTP_PROXY` set would fail the
        # echo run before it measured anything.
        async with FrameProxy(f"ws://127.0.0.1:{port}", fault) as proxy:
            async with websockets.connect(proxy.url, proxy=None) as client:

                async def read() -> None:
                    """Read continuously: never wait for the sender first.

                    Waiting for `sent_all` before the first read made the
                    measured latency the sender's runtime (52.9 ms at 2000
                    frames, 905 ms with a 0.3 s pace). The reader keeps reading
                    with a short timeout and ends only when the sender is done
                    **and** `settle` has passed since the later of send-end and
                    the last frame received.
                    """
                    done_at: float | None = None
                    last_at = asyncio.get_running_loop().time()
                    while True:
                        if done_at is None and sent_all.is_set():
                            done_at = asyncio.get_running_loop().time()
                        if done_at is not None:
                            quiet_since = max(done_at, last_at)
                            left = quiet_since + settle - asyncio.get_running_loop().time()
                            if left <= 0:
                                return
                            timeout = left
                        else:
                            timeout = settle  # a short poll while sending
                        try:
                            raw = await asyncio.wait_for(client.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            if done_at is not None:
                                return
                            continue
                        except websockets.ConnectionClosed as exc:
                            run.closed = type(exc).__name__
                            return
                        try:
                            run.arrive(int(raw))
                        except ValueError:
                            continue  # a frame the harness did not send
                        last_at = asyncio.get_running_loop().time()

                reader = asyncio.create_task(read())
                for seq in range(count):
                    run.send(seq)
                    try:
                        await client.send(str(seq))
                        run.sent_frames += 1  # written, not merely intended
                    except websockets.ConnectionClosed:
                        # The write failed, so this frame never left the
                        # harness: it is neither sent nor lost. Counting it as
                        # lost made the loss one too high on every cut with a
                        # pace (fourth re-check).
                        run.sent.pop()
                        run.closed = "ConnectionClosed during send"
                        break
                    # Yield even without a pace (review point 7): a tight send
                    # loop starved the reader.
                    await asyncio.sleep(send_interval if send_interval else 0)
                sent_all.set()
                await reader
    finally:
        server.close()
        run.ended = time.monotonic()
    return run
