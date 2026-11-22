"""Runs: send messages, fault the bus, measure what arrived.

The harness sends through the bus's own public surface and reads what came
out, so it measures the code under test rather than a copy of its logic. Its
faults are the operating system's where they can be (a signal, a socket
closed under a process) and the proxy's where only timing can produce them
(a duplicated or reordered frame).

`dry_run` exercises the whole flow against a local echo endpoint the harness
starts itself: no Lab, no vexing server, no network. It is what proves the
harness runs where the Lab is unavailable (condition 4 of the approval).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from . import measurands
from .proxy import FrameProxy, ProxyFault


@dataclass
class Sent:
    """One message a run sent, and what became of it."""

    seq: int
    at: float
    payload: str = ""


@dataclass
class Received:
    seq: int
    at: float
    copies: int = 1


@dataclass
class Run:
    """One fault run's bookkeeping and its measurements.

    `sent` and `received` are enough for every `measured` measurand in
    `measurands.toml`: loss, latency, duplicates. The coordinator rows
    (`takeover_*`, `handled_twice_or_missing`) are recorded by the caller in
    `takeovers` and `handled`, since only the caller knows when it killed the
    coordinator and what it then observed.
    """

    name: str
    count: int = 100
    sent: list[Sent] = field(default_factory=list)
    received: dict[int, Received] = field(default_factory=dict)
    takeovers: list[float] = field(default_factory=list)
    handled: dict[str, int] = field(default_factory=dict)  # event id -> times handled
    started: float = 0.0
    ended: float = 0.0

    def send(self, seq: int) -> Sent:
        item = Sent(seq=seq, at=time.monotonic())
        self.sent.append(item)
        return item

    def arrive(self, seq: int) -> None:
        hit = self.received.get(seq)
        if hit is None:
            self.received[seq] = Received(seq=seq, at=time.monotonic())
        else:
            hit.copies += 1

    def measure(self) -> dict[str, dict[str, Any]]:
        """Every measurand in the file, measured or named as not measurable.

        Returns `{name: {"value": ..., "unit": ..., "status": ...}}`, so the
        report is the same shape before and after the switch and a rename in
        the file is a rename everywhere.
        """
        out: dict[str, dict[str, Any]] = {}
        latency = sorted((r.at - self.sent[i].at) * 1000.0
                         for i, r in self.received.items() if i < len(self.sent))
        lost = sum(1 for s in self.sent if s.seq not in self.received)
        dupes = sum(r.copies - 1 for r in self.received.values())
        twice_or_missing = sum(1 for n in self.handled.values() if n != 1)
        values: dict[str, Any] = {
            "loss": lost,
            "latency_p50_ms": round(statistics.median(latency), 1) if latency else None,
            "latency_p95_ms": round(_pct(latency, 95), 1) if latency else None,
            "duplicates": dupes,
            "takeover_p95_s": round(_pct(sorted(self.takeovers), 95), 1) if self.takeovers else None,
            "handled_twice_or_missing": twice_or_missing,
        }
        for m in measurands.load():
            if m.measurable:
                out[m.name] = {"value": values.get(m.name), "unit": m.unit, "status": m.status}
            else:
                out[m.name] = {"value": None, "unit": m.unit, "status": m.status, "why": m.why}
        return out


def _pct(ordered: list[float], p: float) -> float:
    """The p-th percentile of an already ordered list (nearest rank)."""
    if not ordered:
        raise ValueError("empty list")
    rank = max(1, round(p / 100.0 * len(ordered) + 0.5) - 1)
    return ordered[min(rank, len(ordered) - 1)]


async def dry_run(count: int = 20, fault: ProxyFault | None = None) -> Run:
    """The whole flow with no Lab and no server: an echo endpoint the harness
    starts, the proxy in front of it, and the harness's own client reading
    back. A message counts as arrived when its seq comes back."""
    import websockets

    async def echo(conn: Any) -> None:
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    run = Run(name="dry-run", count=count)
    run.started = time.monotonic()
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}", fault) as proxy:
            async with websockets.connect(proxy.url) as client:
                for seq in range(count):
                    run.send(seq)
                    await client.send(str(seq))
                try:
                    while True:
                        raw = await asyncio.wait_for(client.recv(), timeout=0.5)
                        run.arrive(int(raw))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    pass
    finally:
        server.close()
        await server.wait_closed()
        run.ended = time.monotonic()
    return run
