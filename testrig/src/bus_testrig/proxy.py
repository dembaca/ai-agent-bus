"""Faults, injected where they can be injected honestly.

A real Mattermost cannot be made to duplicate or reorder a frame on demand, so
those faults sit in a WebSocket proxy the client talks to instead of the
server. The frames stay the server's own bytes: the proxy forwards what it
received and only decides whether, when and how often the client sees them.

The faults the proxy can produce:

  drop       a frame is discarded
  duplicate  the same frame is delivered twice
  reorder    a frame is held back and released after the next one
  stall      application frames pause for `stall_sec`, then delivery resumes.
             Control frames (ping/pong) are handled by the `websockets` library
             inside each connection and are **not** delayed here, so a heartbeat
             does not see a stall: for "the peer notices the connection is gone"
             use `cut`, and do not read `stall` as a network fault
  blackhole  nothing is delivered any more and the connection is not closed:
             the peer keeps a socket it believes healthy (pings go unanswered)
  cut        the transport is aborted with no close frame, as a dead network
             does: the peer sees the connection drop, not a clean close

Killing a process is the operating system's (`proc`), never simulated
in-process. A network cut is the proxy's `cut`, because what a peer must see is
an aborted transport, and `blackhole`/`cut` need nothing from the OS.

The proxy only speaks WebSocket today. The bus derives REST and WebSocket from
one base URL, so it cannot be put in front of the bus unchanged; passing HTTP
through and faulting only `/api/v4/websocket` belongs to the follow-up PR that
drives the bus (review point 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

KINDS = ("drop", "duplicate", "reorder", "stall", "blackhole", "cut")

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
    matched: int = 0
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

        The server's close code travels to the client (review point 10): a
        proxy that ends every connection with its own 1011 turns "4001, wrong
        key" into "internal error" and hides why a relay was refused. The code
        is read inside the upstream context, where it is set, and carried out.
        """
        from websockets.asyncio.client import connect

        state = ConnState()
        close_code: int | None = None
        close_reason = ""
        try:
            async with connect(self.upstream) as upstream:
                tasks = {asyncio.create_task(self._client_to_upstream(client, upstream)),
                         asyncio.create_task(self._upstream_to_client(upstream, client, state))}
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # A server close arrives two ways: as this connection's own
                # `close_code`, or as the exception its reader got. Both carry
                # the code the client must see (review point 10).
                for task in tasks:
                    if task.cancelled() or task.exception() is None:
                        continue
                    exc = task.exception()
                    rcvd = getattr(exc, "rcvd", None)
                    if rcvd is not None and getattr(rcvd, "code", None) is not None:
                        close_code, close_reason = rcvd.code, rcvd.reason or ""
                        continue
                    raise exc  # type: ignore[misc]
                if close_code is None:
                    close_code = upstream.close_code
                    close_reason = upstream.close_reason or ""
        finally:
            state.cancel()
            if close_code is not None:
                try:
                    await client.close(close_code, close_reason)
                except Exception:  # noqa: BLE001 — the client may be gone already
                    pass

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
            return False  # a frame the predicate excludes is never faulted
        # `every` counts the frames the predicate lets through, not all frames:
        # otherwise a run with `only=` could fault almost nothing.
        state.matched += 1
        return state.matched % fault.every == 0  # every n-th *matching* frame

    async def _deliver(self, frame: Frame, client: Any, state: ConnState) -> None:
        """Deliver one server frame, subject to the fault.

        Invariant: a frame goes out at most once unless the fault is
        `duplicate` (review point 18: a reorder under `every=2` may hold a
        frame that the next applies-check does not see, and the timer must
        then release it — which is why the timer is cancelled whenever the
        held frame is released).
        """
        fault = self.fault
        applies = self._applies(frame, state)  # once per frame, never twice
        if not applies:
            await client.send(frame)
            self.forwarded += 1
            return
        self.altered += 1
        if fault is None:  # unreachable: `_applies` is False without a fault
            return
        if fault.kind == "blackhole":
            return  # silence, and the connection stays open
        if fault.kind == "cut":
            # No close frame: abort the transport the way a dead network does.
            transport = getattr(client, "transport", None)
            if transport is not None:
                transport.abort()
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
