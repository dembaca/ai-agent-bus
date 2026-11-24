"""The console's HTTP side: raw asyncio as the mcp package's hook API
(``hookapi.py``) — no framework — with the same bounds (headers, body,
timeouts, connections) and one thing the hook API never needed: a
server-sent-events stream for ``GET /v1/fleet`` (ADR-015 point 10).

Handlers are ``(path, query, headers) -> Response | Stream``; the SSE
stream holds the connection open and writes an event whenever the fleet
changes plus a keep-alive beat every 15 s.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlsplit

MAX_HEADERS = 64
MAX_LINE = 16 * 1024
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 64
READ_TIMEOUT = 10.0
SSE_KEEPALIVE_SEC = 15.0

REASONS = {200: "OK", 206: "Partial Content", 302: "Found", 303: "See Other",
           400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
           500: "Internal Server Error", 503: "Service Unavailable"}


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
    """The stream the fleet handler returns: the connection writes the first
    event, then one per change, then closes with the client."""
    first: dict
    changes: asyncio.Event
    snapshot: Callable[[], dict]
    cookie: str | None = None  # the session cookie to re-check on the client

    def head(self) -> bytes:
        lines = ["HTTP/1.1 200 OK", "Content-Type: text/event-stream",
                 "Cache-Control: no-cache", "Connection: keep-alive"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


Handler = Callable[[str, dict[str, str], dict[str, str]],
                  Awaitable[Response | SSE]]


@dataclass
class Httpd:
    handler: Handler
    host: str = "127.0.0.1"
    port: int = 0
    _server: object | None = field(default=None, repr=False)
    _conns: set[object] = field(default_factory=set, repr=False)
    _tasks: set[object] = field(default_factory=set, repr=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._spawn, self.host, self.port)

    async def _spawn(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)  # type: ignore[attr-defined]
        task.add_done_callback(self._tasks.discard)  # type: ignore[attr-defined]

    async def stop(self) -> None:
        """The end of the server: the listener closes and every connection's
        task is cancelled, streams included (a stopped console never waits
        on a browser that walked away)."""
        for task in list(self._tasks):  # type: ignore[attr-defined]
            task.cancel()  # type: ignore[attr-defined]
        if self._tasks:  # type: ignore[attr-defined]
            await asyncio.gather(*self._tasks, return_exceptions=True)  # type: ignore[attr-defined]
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]

    async def _serve(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        if len(self._conns) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._conns.add(writer)
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
            except asyncio.TimeoutError:
                return
            parts = line.decode("latin-1").split()
            if len(parts) < 3 or not parts[0].startswith("GET") or len(line) > MAX_LINE:
                writer.write((Response(status=400)).head())
                return
            path = parts[1]
            headers: dict[str, str] = {}
            for _ in range(MAX_HEADERS):
                h = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
                if h in (b"\r\n", b"\n", b""):
                    break
                name, _, value = h.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            query = dict(parse_qsl(urlsplit(path).query))
            route = urlsplit(path).path
            try:
                answer = await self.handler(route, query, headers)
            except (json.JSONDecodeError, ValueError) as exc:
                answer = Response.text(str(exc), 400)
            if isinstance(answer, SSE):
                writer.write(answer.head())
                await write_event(writer, answer.first)
                await stream_loop(writer, answer)
            else:
                writer.write(answer.head())
                writer.write(answer.body)
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        finally:
            self._conns.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def write_event(writer: asyncio.StreamWriter, event: dict) -> None:
    payload = json.dumps(event, separators=(",", ":")).encode()
    writer.write(b"data: " + payload + b"\n\n")
    await writer.drain()


async def stream_loop(writer: asyncio.StreamWriter, sse: SSE) -> None:
    """Hold the stream open: a beat every SSE_KEEPALIVE_SEC, an event
    whenever the fleet changed, the end when the client goes."""
    time.monotonic()
    while True:
        try:
            await asyncio.wait_for(sse.changes.wait(), SSE_KEEPALIVE_SEC)
        except asyncio.TimeoutError:
            sse.changes.clear()
            writer.write(b": keep-alive\n\n")
            await writer.drain()
            time.monotonic()
            continue
        sse.changes.clear()
        await write_event(writer, sse.snapshot())
        time.monotonic()
