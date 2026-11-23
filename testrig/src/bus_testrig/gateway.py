"""One port that speaks both HTTP and the WebSocket upgrade.

The bus derives REST and WebSocket from one base URL (`client.ws_url_from_http`),
so a server that answers only one of them cannot stand in for Mattermost. This
is that server: `asyncio.start_server` on 127.0.0.1, an ephemeral port, HTTP
written by hand.

The `websockets` library cannot be used for the HTTP side: its
`process_request` sees headers only, and a POST with a body makes it answer
before the body is read, so the connection dies. Hence the raw parse here. The
upgrade is a handshake computed by hand too (RFC 6455 §4.2.2), then a session
of frames.

`WsConn` is left easy to drive: `send_text`, `send_frame`, `recv`, `close` and
the underlying `.transport`, so a caller can pause reading on it and observe
what a peer notices. Pings are answered with pongs and a close is answered with
a close by default; a caller that wants silence pauses the transport instead.

Every connection owns its state; nothing here is module-level.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from http import HTTPStatus
from typing import Any, Awaitable, Callable

WEBSOCKET_PATH = "/api/v4/websocket"
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_FRAME = 16 * 1024 * 1024

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

HttpAnswer = tuple[int, dict[str, str], bytes]
OnHttp = Callable[[str, str, dict[str, str], bytes], Awaitable[HttpAnswer]]
OnWs = Callable[["WsConn", dict[str, str]], Awaitable[None]]


class BadRequest(Exception):
    """The bytes on the wire are not an HTTP request this server will answer."""


class WsConn:
    """One WebSocket connection, server side.

    Client frames are read masked and unmasked here; server frames are written
    unmasked. Control frames are handled inside `recv`, so a caller reading
    application frames never has to know a ping arrived — and a caller that
    stops reading (pauses `.transport`) stops answering pings, which is exactly
    what the `halfopen` fault needs.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @property
    def transport(self) -> Any:
        return self._writer.transport

    def __aiter__(self) -> WsConn:
        return self

    async def __anext__(self) -> str | bytes:
        frame = await self.recv()
        if frame is None:
            raise StopAsyncIteration
        opcode, payload = frame
        if opcode == OP_TEXT:
            return payload.decode("utf-8", "replace")
        if opcode == OP_BINARY:
            return payload
        raise StopAsyncIteration  # a close, or a kind this server does not carry

    async def recv(self) -> tuple[int, bytes] | None:
        """The next application or close frame; None at EOF.

        Ping is answered with pong and pong is swallowed, both by default: a
        peer that hides behind a raw socket library still sees a live server.
        """
        while True:
            frame = await self._read_frame()
            if frame is None:
                return None
            opcode, payload = frame
            if opcode == OP_PING:
                await self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code, reason = _close_payload(payload)
                await self.close(code, reason)
                return opcode, payload
            return opcode, payload

    async def send_text(self, text: str) -> None:
        await self.send_frame(OP_TEXT, text.encode("utf-8"))

    async def send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        try:
            self._writer.write(_frame(opcode, payload))
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            self._closed = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame once. The gateway closes the transport after the
        session callback returns; a second call is a no-op."""
        if self._closed:
            return
        self._closed = True
        payload = b"" if code is None else struct.pack("!H", code) + reason.encode("utf-8")
        try:
            self._writer.write(_frame(OP_CLOSE, payload))
            await self._writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def _read_frame(self) -> tuple[int, bytes] | None:
        try:
            first, second = await self._reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        opcode = first & 0x0F
        length = second & 0x7F
        try:
            if length == 126:
                length = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            if length > _MAX_FRAME:
                return None
            mask = await self._reader.readexactly(4) if second & 0x80 else b""
            payload = await self._reader.readexactly(length) if length else b""
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload


def _frame(opcode: int, payload: bytes) -> bytes:
    header = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header += bytes([size])
    elif size < 65536:
        header += bytes([126]) + struct.pack("!H", size)
    else:
        header += bytes([127]) + struct.pack("!Q", size)
    return header + payload


def _close_payload(payload: bytes) -> tuple[int, str]:
    if len(payload) < 2:
        return 1000, ""
    code = struct.unpack("!H", payload[:2])[0]
    return code, payload[2:].decode("utf-8", "replace")


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | None:
    """Method, target (path and query), lower-cased headers, body. None at EOF.

    Raises `BadRequest` for anything it cannot parse; a malformed request is
    answered with 400, never allowed to crash the server.
    """
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError as exc:
        raise BadRequest("request head too long") from exc
    lines = head[:-4].split(b"\r\n")
    try:
        method, target, version = lines[0].decode("latin-1").split(" ")
    except ValueError as exc:
        raise BadRequest("bad request line") from exc
    if not method.isalpha() or not version.startswith("HTTP/") or not target.startswith("/"):
        raise BadRequest("bad request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            raise BadRequest("bad header line")
        headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError as exc:
        raise BadRequest("bad content-length") from exc
    try:
        body = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError as exc:
        raise BadRequest("body shorter than content-length") from exc
    return method, target, headers, body


def _encode_response(status: int, headers: dict[str, str], body: bytes) -> bytes:
    body = body or b""
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Status"
    lines = [f"HTTP/1.1 {status} {reason}"]
    for name, value in headers.items():
        if name.lower() in ("content-length", "transfer-encoding", "connection"):
            continue
        lines.append(f"{name}: {value}")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def _not_found(_method: str, _target: str, _headers: dict[str, str],
                     _body: bytes) -> HttpAnswer:
    return 404, {"content-type": "text/plain"}, b"not found\n"


async def _close_on_connect(conn: WsConn, _headers: dict[str, str]) -> None:
    await conn.close(1000, "")


class Gateway:
    """HTTP and WebSocket on one port, with both sides injectable.

    `on_http` gets (method, target, headers, body) where target carries the
    query string as the request line does; it returns (status, headers, body).
    `on_ws` gets the accepted connection and the request headers.
    """

    def __init__(self, on_http: OnHttp | None = None, on_ws: OnWs | None = None) -> None:
        self.on_http = on_http or _not_found
        self.on_ws = on_ws or _close_on_connect
        self.url = ""
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self) -> Gateway:
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                pass  # a connection a run abandoned must not hold the harness open

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            await self._serve(reader, writer)
        except BadRequest as exc:
            writer.write(_encode_response(400, {"content-type": "text/plain"}, str(exc).encode()))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            request = await _read_request(reader)
            if request is None:
                return
            method, target, headers, body = request
            path = target.split("?", 1)[0]
            if path.endswith(WEBSOCKET_PATH) and headers.get("upgrade", "").lower() == "websocket":
                await self._upgrade(reader, writer, headers)
                return
            status, out_headers, out_body = await self.on_http(method, target, headers, body)
            writer.write(_encode_response(status, out_headers, out_body))
            await writer.drain()
            if headers.get("connection", "").lower() == "close":
                return

    async def _upgrade(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       headers: dict[str, str]) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            raise BadRequest("websocket upgrade without sec-websocket-key")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode("latin-1")).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("latin-1"))
        await writer.drain()
        conn = WsConn(reader, writer)
        try:
            await self.on_ws(conn, headers)
        finally:
            await conn.close(1000, "")
