"""Faults, injected where they can be injected honestly.

Condition 2 of the build order's approval: duplicating a message and
reordering one cannot be provoked at a real Mattermost, so the injection sits
in a WebSocket proxy the bus talks to instead of the server. The frames stay
the server's own bytes: the proxy forwards what it received and only decides
*when* and *how often* the client sees them.

What the proxy can do to the server-to-client direction, and nothing more:

  drop      a frame is discarded (a silently lost message)
  duplicate the same frame is delivered twice
  reorder   a frame is held back and released after the next one
  stall     delivery pauses for `stall_sec`, then resumes

The process faults are not the proxy's: ending the server and cutting the
network are the operating system's, done by a run with a signal and a real
socket (see `proc`). Neither is simulated in-process.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

KINDS = ("drop", "duplicate", "reorder", "stall")


@dataclass
class ProxyFault:
    """What the proxy does to the server's frames.

    `after`: the first n frames pass untouched (the handshake).
    `every`: two or more lets the rest through, so a run can measure both the
    faulted and the unaffected share.
    """

    kind: str
    every: int = 1
    stall_sec: float = 0.5
    reorder_max_wait: float = 0.2
    after: int = 0
    seed: int = 0
    _rng: random.Random = field(init=False, repr=False)
    _n: int = field(default=0, init=False)
    _held: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault {self.kind!r}; one of {KINDS}")
        if self.every < 1:
            raise ValueError("every must be >= 1")
        if self.stall_sec < 0:
            raise ValueError("stall_sec must be >= 0")
        self._rng = random.Random(self.seed)

    def applies(self) -> bool:
        self._n += 1
        return self._n > self.after and self._n % self.every == 0


class FrameProxy:
    """A WebSocket proxy in front of the bus's socket.

    The run hands the bus the proxy's URL instead of the server's, so the bus
    speaks its ordinary WebSocket and does not know anything sits between.
    The client's frames go upstream untouched; only the server's direction is
    faulted.
    """

    def __init__(self, upstream: str, fault: ProxyFault | None = None) -> None:
        self.upstream = upstream
        self.fault = fault
        self.forwarded = 0
        self.altered = 0
        self.received = 0
        self.url = ""
        self._server: Any = None

    async def __aenter__(self) -> FrameProxy:
        from websockets.asyncio.server import serve

        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        # Stop accepting, but do not wait for connections to drain: a run
        # ends with its faults, and a client that dropped half a connection
        # would otherwise hold the harness open. `wait_closed` is bounded.
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — teardown is best effort
            pass

    async def _handle(self, client: Any) -> None:
        """One client connection: a matching connection upstream, frames
        relayed both ways until either side closes."""
        from websockets.asyncio.client import connect

        async with connect(self.upstream) as upstream:
            await asyncio.gather(
                self._client_to_upstream(client, upstream),
                self._upstream_to_client(upstream, client),
            )

    async def _client_to_upstream(self, client: Any, upstream: Any) -> None:
        async for frame in client:
            await upstream.send(frame)

    async def _release_held(self, client: Any) -> None:
        """Send a frame `reorder` is still holding, if any. Called on a timer
        and when the upstream ends, so a held frame is delayed, never lost."""
        fault = self.fault
        if fault is None or fault._held is None:
            return
        held, fault._held = fault._held, None
        try:
            await client.send(held)
            self.forwarded += 1
        except Exception:  # noqa: BLE001 — the client left; that is the run's fault
            return

    async def _upstream_to_client(self, upstream: Any, client: Any) -> None:
        async for frame in upstream:
            self.received += 1
            await self._deliver(frame, client)
        # A frame held by `reorder` when the server stops is still delivered:
        # the fault changes the order, never drops.
        if self.fault is not None and self.fault._held is not None:
            held, self.fault._held = self.fault._held, None
            try:
                await client.send(held)
            except Exception:  # noqa: BLE001 — the client left; the run's fault
                return
            self.forwarded += 1

    async def _deliver(self, frame: Any, client: Any) -> None:
        """Deliver one server frame, subject to `fault`.

        Invariant: every frame the proxy received is sent **exactly once**,
        unless the fault is `duplicate`. A fault changes *when* a frame goes
        out or *whether* it goes out, never how often — which is what makes
        drop, reorder and stall measurable separately from duplication.
        """
        fault = self.fault
        # Nothing is released here on the way in: `reorder` decides below, so
        # that a held frame really swaps with its successor instead of being
        # flushed just before it.
        if fault is None or not fault.applies():
            await client.send(frame)
            self.forwarded += 1
            return
        self.altered += 1
        if fault.kind == "drop":
            return
        if fault.kind == "duplicate":
            await client.send(frame)
            await client.send(frame)
            self.forwarded += 2
            return
        if fault.kind == "reorder":
            # Swap this frame with the next one: the held frame goes out
            # second. If no next frame comes, the timer releases it, so a
            # trailing frame is delayed, never lost.
            if fault._held is None:
                fault._held = frame
                asyncio.get_running_loop().call_later(
                    fault.reorder_max_wait,
                    lambda: asyncio.ensure_future(self._release_held(client)))
                return
            held, fault._held = fault._held, None
            await client.send(frame)
            await client.send(held)
            self.forwarded += 2
            return
        if fault.kind == "stall":
            await asyncio.sleep(fault.stall_sec)
            await client.send(frame)
            self.forwarded += 1
