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
  blackhole  server-to-client delivery stops and the connection is not closed:
             the client keeps a socket it believes healthy. Only that one
             direction goes silent, pings are still answered, so a heartbeat
             does not notice
  halfopen   both directions stop being read (pause_reading on both
             transports), and the connection is not closed: the client's pings
             reach a proxy that no longer reads them, so no pong comes back and
             a heartbeat **does** notice. Writing is still possible, which is
             the difference between a frozen line and a cut
  cut        the transport is aborted with no close frame, as a dead network
             does: the peer sees the connection drop, not a clean close

Killing a process is the operating system's (`proc`), never simulated
in-process. A network cut is the proxy's `cut`, because what a peer must see is
an aborted transport, and `blackhole`/`cut`/`halfopen` need nothing from the OS.

There are two proxies. `FrameProxy` speaks WebSocket only: it stands in front of
a WebSocket endpoint. `GatewayProxy` speaks the bus's whole base URL — HTTP
passed through unchanged, only `/api/v4/websocket` faulted — because the bus
derives REST and WebSocket from one URL (`client.ws_url_from_http`) and cannot
be pointed at a WebSocket-only proxy. Both share one implementation of the
faults (`FaultRelay`), so a frame is treated the same however it got here
(review point 2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .gateway import Gateway, WsConn

KINDS = ("drop", "duplicate", "reorder", "stall", "blackhole", "halfopen", "cut")

# The state `reorder` keeps is per connection, not per proxy (review point 9):
# two clients through one proxy must not swap each other's frames.
Frame = Any
Send = Callable[[Frame], Awaitable[None]]
Abort = Callable[[], None]

# Headers a proxy does not forward (RFC 7230 §6.1): they describe one hop.
_HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-authenticate",
                         "proxy-authorization", "te", "trailers",
                         "transfer-encoding", "upgrade"))

# How long the proxy waits for the upstream's close handshake. A `halfopen`
# line has stopped reading, so that handshake never completes; the wait must be
# bounded or every frozen run would hang for the library's default 10 s.
CLOSE_SEC = 2.0


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


@dataclass
class FaultRelay:
    """The faults, once, for every proxy that relays a server's frames.

    The counters are the proxy's (`received`, `forwarded`, `altered`); the
    decision state is the connection's (`ConnState`), because `reorder` holds a
    frame and the hold belongs to one connection (review point 9).
    """

    fault: ProxyFault | None = None
    received: int = 0
    forwarded: int = 0
    altered: int = 0

    def applies(self, frame: Frame, state: ConnState) -> bool:
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

    async def deliver(self, frame: Frame, state: ConnState, *, send: Send,
                      abort: Abort | None = None, freeze: Abort | None = None) -> None:
        """Deliver one server frame, subject to the fault.

        Invariant: a frame goes out at most once unless the fault is
        `duplicate` (review point 18: a reorder under `every=2` may hold a
        frame that the next applies-check does not see, and the timer must
        then release it — which is why the timer is cancelled whenever the
        held frame is released).
        """
        fault = self.fault
        applies = self.applies(frame, state)  # once per frame, never twice
        if not applies:
            await send(frame)
            self.forwarded += 1
            return
        self.altered += 1
        if fault is None:  # unreachable: `applies` is False without a fault
            return
        if fault.kind == "blackhole":
            return  # silence, and the connection stays open
        if fault.kind == "halfopen":
            # Both directions stop being read; the transport stays open, so
            # what the peer observes is silence, not a close. This frame and
            # everything after it are not delivered: a frozen line carries
            # nothing (use `after` to let the handshake through first).
            if freeze is not None:
                freeze()
            return
        if fault.kind == "cut":
            # No close frame: abort the transport the way a dead network does.
            if abort is not None:
                abort()
            return
        if fault.kind == "drop":
            return
        if fault.kind == "duplicate":
            await send(frame)
            await send(frame)
            self.forwarded += 2
            return
        if fault.kind == "reorder":
            if state.held is None:
                state.held = frame
                state.release = asyncio.get_running_loop().call_later(
                    fault.reorder_max_wait,
                    lambda: asyncio.ensure_future(self.flush(state, send=send)))
                return
            held, state.held = state.held, None
            state.cancel()
            await send(frame)
            await send(held)
            self.forwarded += 2
            return
        if fault.kind == "stall":
            await asyncio.sleep(fault.stall_sec)
            await send(frame)
            self.forwarded += 1

    async def flush(self, state: ConnState, *, send: Send) -> None:
        """Release a frame `reorder` still holds when the server stops.

        A frame held by `reorder` whose successor never came is sent by its
        timer, so a trailing frame is delayed, never lost.
        """
        if state.held is None:
            return
        held, state.held = state.held, None
        state.cancel()
        try:
            await send(held)
            self.forwarded += 1
        except Exception:  # noqa: BLE001 — the client left; that is the run's fault
            return


def _abort_of(target: Any) -> Abort:
    transport = getattr(target, "transport", None)
    return transport.abort if transport is not None else (lambda: None)


def _ws_url(http_url: str) -> str:
    """The WebSocket URL a base URL implies, as `client.ws_url_from_http` does."""
    parsed = urlsplit(http_url.strip().rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/api/v4"):
        path += "/api/v4"
    return urlunsplit((scheme, parsed.netloc, path + "/websocket", "", ""))


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
        self.relay = FaultRelay(fault)
        self.url = ""
        self._server: Any = None

    @property
    def received(self) -> int:
        return self.relay.received

    @property
    def forwarded(self) -> int:
        return self.relay.forwarded

    @property
    def altered(self) -> int:
        return self.relay.altered

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
        except Exception as exc:  # noqa: BLE001 — a cut ends the connection; that is the fault
            from websockets.exceptions import ConnectionClosed

            if not isinstance(exc, (ConnectionClosed, ConnectionResetError, OSError)):
                raise
        finally:
            state.cancel()
            if close_code is not None:
                try:
                    await client.close(close_code, close_reason)
                except Exception:  # noqa: BLE001 — the client may be gone already
                    pass
            else:
                # The upstream ended without a close frame: it died, and the
                # client must see an abort, not a clean 1000 (third re-check,
                # point 3). A dead server is not a polite goodbye.
                _abort_of(client)()

    async def _client_to_upstream(self, client: Any, upstream: Any) -> None:
        async for frame in client:
            await upstream.send(frame)

    async def _upstream_to_client(self, upstream: Any, client: Any, state: ConnState) -> None:
        async for frame in upstream:
            self.relay.received += 1
            await self.relay.deliver(frame, state, send=client.send,
                                      abort=_abort_of(client),
                                      freeze=_freeze(client, upstream))
        await self.relay.flush(state, send=client.send)


def _freeze(*targets: Any) -> Abort:
    """Pause reading on every target: the line is open and carries nothing.

    The transports stay open, so a peer that writes sees no error; a peer that
    waits for a pong waits until its own heartbeat gives up.
    """
    def freeze() -> None:
        for target in targets:
            transport = getattr(target, "transport", None)
            if transport is not None and not transport.is_closing():
                transport.pause_reading()
    return freeze


class GatewayProxy:
    """The bus's base URL in front of Mattermost: HTTP through, WebSocket faulted.

    The client is pointed at the proxy's `url`, so REST and WebSocket both go
    through one port, exactly as they go to one Mattermost. Every HTTP request
    is forwarded unchanged; only the upgrade is proxied frame by frame, with the
    same `FaultRelay` as `FrameProxy`.
    """

    def __init__(self, upstream: str, fault: ProxyFault | None = None) -> None:
        self.upstream = upstream.rstrip("/")
        self.ws_upstream = _ws_url(upstream)
        self.fault = fault
        self.relay = FaultRelay(fault)
        self.url = ""
        self._gateway: Gateway | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def received(self) -> int:
        return self.relay.received

    @property
    def forwarded(self) -> int:
        return self.relay.forwarded

    @property
    def altered(self) -> int:
        return self.relay.altered

    async def __aenter__(self) -> GatewayProxy:
        self._client = httpx.AsyncClient(timeout=10.0)
        self._gateway = Gateway(self._on_http, self._on_ws)
        await self._gateway.start()
        self.url = self._gateway.url
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._gateway is not None:
            await self._gateway.stop()
        if self._client is not None:
            await self._client.aclose()

    async def _on_http(self, method: str, target: str, headers: dict[str, str],
                       body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Forward one request unchanged; the client's own pipeline decides
        how the answer is read, so status, headers and body come back as sent.

        `httpx` decodes a compressed upstream body while reading it, so
        `content-encoding` is dropped with `content-length`: the bytes handed
        on are already decoded, and saying otherwise would make the caller
        decode twice."""
        client = self._client
        if client is None:
            raise RuntimeError("GatewayProxy is not entered")
        forwarded = {name: value for name, value in headers.items()
                     if name.lower() not in _HOP_BY_HOP and name.lower() not in
                     ("host", "content-length")}
        try:
            response = await client.request(method, self.upstream + target,
                                            headers=forwarded, content=body)
        except httpx.HTTPError as exc:
            # An upstream that cannot be reached is an answer too (review point
            # at proxy.py:419): closing without one left the caller with a bare
            # read error, which reads like a bug in the client rather than the
            # upstream being gone.
            return 502, {"content-type": "text/plain"}, (
                f"the proxy could not reach the backend: {type(exc).__name__}: {exc}\n"
            ).encode()
        out = {name: value for name, value in response.headers.items()
               if name.lower() not in _HOP_BY_HOP and name.lower() not in
               ("content-length", "content-encoding")}
        return response.status_code, out, response.content

    async def _on_ws(self, conn: WsConn, _headers: dict[str, str]) -> None:
        """One upgraded connection: the same relay and close handling as
        `FrameProxy._handle`, with the raw gateway connection downstream."""
        from websockets.asyncio.client import connect

        state = ConnState()
        close_code: int | None = None
        close_reason = ""
        try:
            # `close_timeout` bounded: a `halfopen` upstream has stopped reading,
            # so its close handshake never completes (CLOSE_SEC).
            async with connect(self.ws_upstream, close_timeout=CLOSE_SEC) as upstream:
                tasks = {asyncio.create_task(self._client_to_upstream(conn, upstream)),
                         asyncio.create_task(self._upstream_to_client(upstream, conn, state))}
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
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
        except Exception as exc:  # noqa: BLE001 — a cut ends the connection; that is the fault
            from websockets.exceptions import ConnectionClosed

            if not isinstance(exc, (ConnectionClosed, ConnectionResetError, OSError)):
                raise
        finally:
            state.cancel()
            if close_code is not None:
                await conn.close(close_code, close_reason)
            else:
                # The upstream died without a close frame: abort, as FrameProxy
                # does, so a killed server is not read as a clean goodbye.
                conn.transport.abort()

    async def _client_to_upstream(self, conn: WsConn, upstream: Any) -> None:
        async for frame in conn:
            await upstream.send(frame)

    async def _upstream_to_client(self, upstream: Any, conn: WsConn, state: ConnState) -> None:
        async def send(frame: Frame) -> None:
            if isinstance(frame, bytes):
                await conn.send_frame(0x2, frame)
            else:
                await conn.send_text(frame)

        async for frame in upstream:
            self.relay.received += 1
            await self.relay.deliver(frame, state, send=send,
                                      abort=_abort_of(conn),
                                      freeze=_freeze(conn, upstream))
        await self.relay.flush(state, send=send)
