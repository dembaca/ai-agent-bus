"""Requests to the bridge (ADR-014 phase 5, plan B): the subjects for what an agent
needs from the bridge but that is no channel message.

An agent publishes on ``<req>.<kind>.<agent>``, ``<kind>`` one of ``history``, ``file``
and ``header``; the agent's name is the last token, as on every bus subject, and the
credential lets it publish there only under its own name (``callout/perms.go``). The
bridge answers on ``deliver.<agent>.req.<request id>``, which only the agent's own
subscription right covers. No stream carries either subject, so nothing is kept: a
lost message fails the request and the caller gets an error, never a silent success.

The body is JSON, ``{"v": 1, "request": "<id>", "kind": "<kind>", "payload": ...}``,
the answer ``{"v": 1, "request": "<id>", "kind": "<kind>", "error": "..."}`` or the
same with a result the kind defines. An answer may come in parts: each carries
``part`` (from 1) and ``last``, and the call reads until a part says ``last``. The request id is 16 characters of ``[a-z0-9]``
and is made here, since the agent has no inbox right: the bridge never answers onto a
subject the request names.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Mapping

from .natsbus import MAX_BODY, NatsBus, NatsBusError

REQUEST_SEC = 5.0  # how long a request waits for the bridge's answer, in all
KINDS = ("history", "file", "header")
MAX_PARTS = 64  # an answer's parts: a file's chunks or a history read's pages
MAX_TOTAL = 4 * 1024 * 1024  # the bytes an answer's parts may add up to
_REQUEST_ID = 16


class RequestError(NatsBusError):
    """A request did not get an answer: the bridge is away, answered "not built" for
    that kind, or the answer did not hold together. The text never holds a token."""


def _new_id() -> str:
    """16 characters of ``[a-z0-9]``: ``hex`` is 4 bits a character, so 8 bytes."""
    return secrets.token_hex(8)


def _decode_answer(body: bytes | bytearray, request: str, kind: str) -> dict[str, Any]:
    try:
        answer = json.loads(bytes(body))
    except (ValueError, UnicodeDecodeError):
        raise RequestError("the bridge's answer is not JSON") from None
    if not isinstance(answer, dict) or answer.get("v") != 1:
        raise RequestError("the bridge's answer is not a request answer")
    if answer.get("request") != request:
        raise RequestError("the bridge's answer belongs to another request")
    if answer.get("kind") != kind:
        raise RequestError("the bridge's answer names another kind")
    error = answer.get("error")
    if error is not None:
        raise RequestError(str(error)[:200])
    return answer


async def request(bus: NatsBus, kind: str, payload: Mapping[str, Any] | None = None, *,
                  timeout: float = REQUEST_SEC, prefix: str | None = None) -> dict[str, Any]:
    """Ask the bridge for one ``kind`` and collect its answer (``REQUEST_SEC``).

    An answer may come in parts: each carries ``part`` (1, 2, ...) and ``last``, and
    the call reads until a part says ``last`` - one request, one deadline. One part
    returns its ``result`` as before; several return ``{"parts": [result, ...]}``, the
    results in part order, so a file download or a long history read gets the whole
    answer. A gap in the numbering, a part that names another request or kind, and a
    stream that never ends (no ``last`` before the deadline) are errors, as is a
    bridge that answers with an ``error``.

    Raises ``RequestError`` when the bus is not connected, when nothing answers within
    ``timeout``, when the bridge answers with an error (the scaffold answers "not
    built" for every kind), or when an answer does not hold together.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown request kind {kind!r}; one of {', '.join(KINDS)}")
    nc = bus._nc  # noqa: SLF001 - the bus keeps the connection; a request is its own subject
    if nc is None or not bus._connected:  # noqa: SLF001
        raise RequestError("the bus is not connected")
    request_id = _new_id()
    prefix = prefix or bus._s.req  # noqa: SLF001 - the settings the bus was made with
    subject = f"{prefix}.{kind}.{bus.name}"
    answer_subject = f"deliver.{bus.name}.req.{request_id}"
    body = json.dumps({"v": 1, "request": request_id, "kind": kind,
                       "payload": dict(payload or {})}, separators=(",", ":")).encode()
    if len(body) > MAX_BODY:
        raise ValueError("the request's body is larger than a NATS message may be")
    answers: asyncio.Queue[Any] = asyncio.Queue()

    async def on_answer(msg: Any) -> None:
        answers.put_nowait(msg)

    sub = await nc.subscribe(answer_subject, cb=on_answer)
    try:
        await nc.flush()  # subscribed before the request goes out
        await bus._publish(subject, body)  # noqa: SLF001 - same path as a post, same rights check
        deadline = time.monotonic() + timeout
        parts: list[Any] = []
        total = 0
        expected = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if parts:
                    raise RequestError(
                        f"the bridge's {kind} answer stopped after part {expected - 1}") from None
                raise RequestError(
                    f"the bridge did not answer a {kind} request within {timeout:.0f} s") from None
            try:
                msg = await asyncio.wait_for(answers.get(), remaining)
            except asyncio.TimeoutError:
                if parts:
                    raise RequestError(
                        f"the bridge's {kind} answer stopped after part {expected - 1}") from None
                raise RequestError(
                    f"the bridge did not answer a {kind} request within {timeout:.0f} s") from None
            data = getattr(msg, "data", b"")
            total += len(data) if isinstance(data, (bytes, bytearray)) else 0
            if total > MAX_TOTAL:
                raise RequestError(f"the bridge's {kind} answer is larger than {MAX_TOTAL} bytes")
            answer = _decode_answer(data if isinstance(data, (bytes, bytearray)) else b"",
                                    request_id, kind)
            part = answer.get("part", 1)
            if part != expected:
                raise RequestError(f"the bridge's {kind} answer has part {part}, expected {expected}")
            parts.append(answer.get("result"))
            if answer.get("last", True):
                break
            if len(parts) >= MAX_PARTS:
                raise RequestError(f"the bridge's {kind} answer has more than {MAX_PARTS} parts")
            expected += 1
        return parts[0] if len(parts) == 1 else {"parts": parts}
    finally:
        try:
            await sub.unsubscribe()
        except Exception:  # noqa: BLE001 - the connection may be gone; the answer is what counts
            pass
