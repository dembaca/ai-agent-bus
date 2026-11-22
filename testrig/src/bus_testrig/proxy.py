"""Faults, injected where they can be injected honestly.

A real Mattermost cannot be made to duplicate or reorder a frame on demand, so
those faults sit in a WebSocket proxy the client talks to instead of the
server. The frames stay the server's own bytes: the proxy forwards what it
received and only decides whether, when and how often the client sees them.

The faults the proxy can produce:

  drop       a frame is discarded
  duplicate  the same frame is delivered twice
  reorder    a frame is held back and released after the next one
  stall      delivery pauses for `stall_sec`, then resumes (pongs too, so a
             heartbeat sees the silence)
  blackhole  nothing is delivered any more and the connection is not closed:
             the peer keeps a socket it believes healthy

The process faults are not the proxy's: killing a process and cutting its
network are the operating system's (`proc`), never simulated in-process.

The proxy only speaks WebSocket today. The bus derives REST and WebSocket from
one base URL, so it cannot be put in front of the bus unchanged; passing HTTP
through and faulting only `/api/v4/websocket` belongs to the follow-up PR that
drives the bus (review point 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

KINDS = ("drop", "duplicate", "reorder", "stall", "blackhole")

# The state `reorder` keeps is per connection, not per proxy (review point 9):
# two clients through one proxy must not swap each other's frames.
Frame = Any


@dataclass
class ProxyFault:
    """What the proxy does to the server's frames.

    `after`: the first n frames pass untouched (the handshake).
    `every`: two or more lets the rest through, so a run can measure both the
    faulted and the unaffected share.
    `only`: a predicate on the frame; `None` faults every frame that passes
    `every`. Deciding *which* frames to fault belongs here, not in an unused
    random seed (review point 14).
    """

    kind: str
    every: int = 1
    stall_sec: float = 0.5
    reorder_max_wait: float = 0.2
    after: int = 0
    only: Callable[[Frame], bool] | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault {self.kind!r}; one of {KINDS}")
        if self.every < 1:
            raise ValueError("every must be >= 1")
        if self.stall_sec < 0:
            raise ValueError("stall_sec must be >= 0")


@dataclass
class ConnState:
    """One connection's fault state: the count and a frame held for reorder."""

    n: int = 0
    held: Frame = None
    release: asyncio.TimerHandle | None = None

    def cancel(self) -> None:
        if self.release is not None:
            self.release.cancel()
            self.release = None


class FrameProxy:
    """A WebSocket proxy in front of a WebSocket server, for frame faults.

    The client is handed the proxy's URL instead of the server's, so it speaks
    its ordinary WebSocket and does not know anything sits between. The
    client's frames go upstream untouched; only the server's direction is
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
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass  # a connection a run abandoned must not hold the harness open

    async def _handle(self, client: Any) -> None:
        """One client connection: a matching connection upstream, frames
        relayed both ways until **either** side closes.

        `gather` used to wait for both directions, so a client that left kept
        the proxy waiting for a server that stayed (review point 10). Waiting
        for the first one to finish and then closing the other is right: a
        connection is over when either end says so.
        """
        from websockets.asyncio.client import connect

        state = ConnState()
        try:
            async with connect(self.upstream) as upstream:
                tasks = {asyncio.create_task(self._client_to_upstream(client, upstream)),
                         asyncio.create_task(self._upstream_to_client(upstream, client, state))}
                _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in tasks:
                    if not task.cancelled() and task.exception() is not None:
                        raise task.exception()  # type: ignore[misc]
        finally:
            state.cancel()

    async def _client_to_upstream(self, client: Any, upstream: Any) -> None:
        async for frame in client:
            await upstream.send(frame)

    async def _upstream_to_client(self, upstream: Any, client: Any, state: ConnState) -> None:
        async for frame in upstream:
            self.received += 1
            await self._deliver(frame, client, state)
        await self._flush(client, state)

    async def _flush(self, client: Any, state: ConnState) -> None:
        """Release a frame `reorder` still holds when the server stops."""
        if state.held is None:
            return
        held, state.held = state.held, None
        state.cancel()
        try:
            await client.send(held)
            self.forwarded += 1
        except Exception:  # noqa: BLE001 — the client left; that is the run's fault
            return

    def _applies(self, frame: Frame, state: ConnState) -> bool:
        fault = self.fault
        if fault is None:
            return False
        state.n += 1
        if state.n <= fault.after:
            return False
        if fault.only is not None and not fault.only(frame):
            return False
        return state.n % fault.every == 0

    async def _deliver(self, frame: Frame, client: Any, state: ConnState) -> None:
        """Deliver one server frame, subject to the fault.

        Invariant: a frame goes out at most once unless the fault is
        `duplicate` (review point 18: a reorder under `every=2` may hold a
        frame that the next applies-check does not see, and the timer must
        then release it — which is why the timer is cancelled whenever the
        held frame is released).
        """
        fault = self.fault
        if fault is not None and fault.kind == "blackhole" and self._applies(frame, state):
            self.altered += 1
            return  # silence, and the connection stays open
        if not self._applies(frame, state):
            await client.send(frame)
            self.forwarded += 1
            return
        self.altered += 1
        if fault is None:  # unreachable: `_applies` is False without a fault
            return
        if fault.kind == "drop":
            return
        if fault.kind == "duplicate":
            await client.send(frame)
            await client.send(frame)
            self.forwarded += 2
            return
        if fault.kind == "reorder":
            if state.held is None:
                state.held = frame
                state.release = asyncio.get_running_loop().call_later(
                    fault.reorder_max_wait,
                    lambda: asyncio.ensure_future(self._release(client, state)))
                return
            held, state.held = state.held, None
            state.cancel()
            await client.send(frame)
            await client.send(held)
            self.forwarded += 2
            return
        if fault.kind == "stall":
            await asyncio.sleep(fault.stall_sec)
            await client.send(frame)
            self.forwarded += 1

    async def _release(self, client: Any, state: ConnState) -> None:
        """A frame held by `reorder` whose successor never came: send it, so a
        trailing frame is delayed, never lost."""
        await self._flush(client, state)
