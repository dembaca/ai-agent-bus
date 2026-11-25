"""The console's HTTP side: raw asyncio as the mcp package's hook API
(``hookapi.py``) — no framework — with the same bounds and, for the
reviews, harder edges: one deadline for the whole request head (a slow
line is a slow request, not ten slow ones), a bounded drain, streams that
end when the session behind them ends, and per-stream events.

Handlers return ``(path, query, headers) -> Response | SSE``, the method
(GET, or POST for the logout) in the pseudo-header ``:method``; an SSE
holds the connection open and writes an event whenever the fleet changes,
a keep-alive beat every ``SSE_KEEPALIVE_SEC``, and closes when the
session is gone (the ``alive`` callback) or the browser walked away.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

MAX_HEADERS = 64
MAX_LINE = 16 * 1024
HEAD_DEADLINE = 10.0     # the whole request head, one deadline
DRAIN_SEC = 5.0          # one write may not block the server longer
MAX_CONNECTIONS = 64     # before the login; a stream cap comes per session
SSE_KEEPALIVE_SEC = 15.0
MAX_POST_BODY = 4096     # the logout's form is empty; nothing more is read

REASONS = {200: "OK", 206: "Partial Content", 303: "See Other",
           400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 411: "Length Required",
           413: "Payload Too Large",
           429: "Too Many Requests", 500: "Internal Server Error",
           502: "Bad Gateway", 503: "Service Unavailable"}


class Refusal(ValueError):
    """A request refused before any handler, with its own status."""

    def __init__(self, status: int, why: str) -> None:
        super().__init__(why)
        self.status = status


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()

    def head(self) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {REASONS.get(self.status, '')}"]
        lines.append(f"Content-Type: {self.content_type}")
        lines.append(f"Content-Length: {len(self.body)}")
        lines.append("Connection: close")
        lines += [f"{k}: {v}" for k, v in self.headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    def json(data: object, status: int = 200,
             headers: tuple[tuple[str, str], ...] = ()) -> "Response":
        return Response(status=status, body=json.dumps(data).encode(),
                        content_type="application/json", headers=headers)

    @staticmethod
    def see(url: str) -> "Response":
        return Response(status=303, body=b"", content_type="text/plain",
                        headers=(("Location", url),))

    @staticmethod
    def text(body: str, status: int = 200,
             content_type: str = "text/plain; charset=utf-8") -> "Response":
        return Response(status=status, body=body.encode(), content_type=content_type)


@dataclass
class SSE:
    """The stream the fleet handler returns. ``first`` is one event, every
    later wave a list of events (one per change — the app decodes
    ``snapshot``/``card``/``drop`` and nothing else). ``alive`` is asked
    on every keep-alive: a session that ended mid-stream ends the stream;
    ``teardown`` runs when it ends, however it ends."""
    first: dict
    changes: asyncio.Event
    snapshot: Callable[[], list[dict[str, Any]]] = lambda: []
    alive: Callable[[], bool] | None = None
    teardown: Callable[[], None] | None = None

    def head(self) -> bytes:
        lines = ["HTTP/1.1 200 OK", "Content-Type: text/event-stream",
                 "Cache-Control: no-store",
                 "X-Content-Type-Options: nosniff",
                 "Connection: keep-alive"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    beat: Callable[[], list[dict]] | None = None
    "The timed beat: when the keep-alive would otherwise send only a" \
    " comment, this returns events to send instead (aging changes no" \
    " delivery carries); None keeps the plain beat."


Handler = Callable[[str, dict[str, str], dict[str, str]], Awaitable[Response | SSE]]


@dataclass
class Httpd:
    handler: Handler
    host: str = "127.0.0.1"
    port: int = 0
    _server: asyncio.AbstractServer | None = field(default=None, repr=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._spawn, self.host, self.port)

    async def _spawn(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        """The end of the server: the listener closes and every connection's
        task is cancelled, streams included (a stopped console never waits
        on a browser that walked away)."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        sockets = getattr(self._server, "sockets", None)
        assert sockets, "the http server bound no socket"
        return sockets[0].getsockname()[1]

    async def _serve(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        if len(self._tasks) > MAX_CONNECTIONS:
            writer.close()
            return
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), HEAD_DEADLINE)
                if len(line) > MAX_LINE:
                    raise ValueError("request line too long")
                parts = line.decode("latin-1").split()
                if len(parts) < 3 or parts[0] not in ("GET", "POST"):
                    raise ValueError("only GET and POST")
                headers: dict[str, str] = {}
                async with asyncio.timeout(HEAD_DEADLINE):
                    while True:
                        h = await reader.readline()
                        if h in (b"\r\n", b"\n", b""):
                            break
                        if len(h) > MAX_LINE or len(headers) >= MAX_HEADERS:
                            raise ValueError("head too big")
                        name, _, value = h.decode("latin-1").partition(":")
                        headers[name.strip().lower()] = value.strip()
                    if parts[0] == "POST":
                        # a POST carries no body the console reads (the
                        # logout's form is empty): a small one is drained;
                        # a chunked one has no length to bound it by
                        if "transfer-encoding" in headers:
                            raise Refusal(411, "POST without a Content-Length")
                        length = headers.get("content-length", "0")
                        if not length.isdigit():
                            raise ValueError("Content-Length is no number")
                        if int(length) > MAX_POST_BODY:
                            raise Refusal(413, "POST body too big")
                        if int(length):
                            await reader.readexactly(int(length))
                # the method as a pseudo-header: no line of the head can
                # name it (a leading ":" leaves an empty name)
                headers[":method"] = parts[0]
            except (asyncio.TimeoutError, ValueError, ConnectionError,
                    asyncio.IncompleteReadError) as exc:
                writer.write(Response(status=getattr(exc, "status", 400)).head())
                return
            query = dict(parse_qsl(urlsplit(parts[1]).query))
            route = urlsplit(parts[1]).path
            try:
                answer = await self.handler(route, query, headers)
            except (json.JSONDecodeError, ValueError) as exc:
                answer = Response.text(str(exc), 400)
            if isinstance(answer, SSE):
                writer.write(answer.head())
                await write_event(writer, [answer.first])
                try:
                    await stream_loop(writer, answer)
                finally:
                    if answer.teardown is not None:
                        answer.teardown()
                return
            writer.write(answer.head())
            writer.write(answer.body)
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def write_event(writer: asyncio.StreamWriter,
                       events: list[dict[str, Any]]) -> None:
    for event in events:
        payload = json.dumps(event, separators=(",", ":")).encode()
        writer.write(b"data: " + payload + b"\n\n")
    await asyncio.wait_for(writer.drain(), DRAIN_SEC)


async def stream_loop(writer: asyncio.StreamWriter, sse: SSE) -> None:
    """Hold the stream open: a beat every ``SSE_KEEPALIVE_SEC`` (asking
    ``alive``: a session that ended ends the stream), an event wave
    whenever the fleet changed, the end when the client goes."""
    while True:
        try:
            await asyncio.wait_for(sse.changes.wait(), SSE_KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            if sse.alive is not None and not sse.alive():
                return  # logged out or expired: the stream ends with it
            sse.changes.clear()
            if sse.beat is not None and sse.snapshot is not None:
                # the beat carries the cards that aging changed (a dead
                # agent goes offline without a delivery carrying it);
                # an empty diff keeps the plain comment
                events = sse.beat()
            else:
                events = []
            if events:
                await write_event(writer, events)
            else:
                writer.write(b": keep-alive\n\n")
                await write_event(writer, [])
            continue
        sse.changes.clear()
        if sse.alive is not None and not sse.alive():
            return  # the session ended: the busyness never outlives it
        await write_event(writer, sse.snapshot())
