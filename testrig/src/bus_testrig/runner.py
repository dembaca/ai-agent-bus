"""Runs: send frames, fault the channel, measure what arrived.

This harness measures **its own echo loop** today: a real `websockets` server,
the real proxy in front of it and the harness's own client. It does not drive
the bus yet — that is the follow-up PR, and every text here says so rather
than implying otherwise (review point 1).

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
            "commit": self.commit,
            "duration_s": round(self.ended - self.started, 3) if self.ended else None,
            "measured": self.measure(),
        }


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
    import websockets

    async def echo(conn: Any) -> None:
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    run = Run(name="echo-run", count=count, target="echo",
              fault=fault.kind if fault else "none",
              fault_params={"every": fault.every, "after": fault.after,
                            "stall_sec": fault.stall_sec,
                            "reorder_max_wait": fault.reorder_max_wait} if fault else {},
              send_interval=send_interval)
    run.started = time.monotonic()
    stall = fault.stall_sec if fault is not None and fault.kind == "stall" else 0.0
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}", fault) as proxy:
            async with websockets.connect(proxy.url) as client:

                async def read() -> None:
                    # Read past completeness (review point 1): stopping at the
                    # first sighting of every seq undercounted duplicates by
                    # one per frame, because the copies arrive after it. The
                    # run ends on quiet, not on completeness.
                    quiet = asyncio.get_running_loop().time()
                    # The window must outlast the longest expected silence: a
                    # stall of `stall_sec` is silence, not the end of the run
                    # (review point 5, which a fixed 0.2 s window broke again).
                    settle = max(0.2, stall + 0.3)
                    while True:
                        try:
                            raw = await asyncio.wait_for(client.recv(), timeout=quiet + settle
                                                         - asyncio.get_running_loop().time())
                        except asyncio.TimeoutError:
                            return
                        try:
                            run.arrive(int(raw))
                        except ValueError:
                            continue  # a frame the harness did not send
                        quiet = asyncio.get_running_loop().time()

                reader = asyncio.create_task(read())
                for seq in range(count):
                    run.send(seq)
                    await client.send(str(seq))
                    # Yield even without a pace (review point 7): a tight send
                    # loop starved the reader, so the measured latency was the
                    # sender's queue, not the channel's.
                    await asyncio.sleep(send_interval if send_interval else 0)
                await reader
    finally:
        server.close()
        run.ended = time.monotonic()
    return run
