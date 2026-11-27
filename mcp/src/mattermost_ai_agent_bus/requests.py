"""Requests to the bridge (ADR-014 phase 5, plan B; RT-6.44): the subjects for what an
agent needs from the bridge but that is no channel message.

An agent publishes on ``<req>.<kind>.<agent>``, ``<kind>`` one of ``history``, ``file``
and ``header``; the agent's name is the last token, as on every bus subject, and the
credential lets it publish there only under its own name (``callout/perms.go``). The
bridge answers on ``deliver.<agent>.req.<request id>``, which only the agent's own
subscription right covers. No stream carries either subject, so nothing is kept: a
lost message fails the request and the caller gets an error, never a silent success.

The body is JSON, ``{"v": 1, "request": "<id>", "kind": "<kind>", "payload": ...}``,
the answer ``{"v": 1, "request": "<id>", "kind": "<kind>", "error": "..."}`` or the
same with a result the kind defines. The request id is 16 characters of ``[a-z0-9]``
and is made here, since the agent has no inbox right: the bridge never answers onto a
subject the request names.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Mapping

from .natsbus import MAX_BODY, NatsBus, NatsBusError

REQUEST_SEC = 5.0  # how long a request waits for the bridge's answer
REQUEST_PREFIX = "req"  # the request subjects' prefix (CB_AUTH_REQ_PREFIX, CB_BRIDGE_REQ_PREFIX)
KINDS = ("history", "file", "header")
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
                  timeout: float = REQUEST_SEC, prefix: str = REQUEST_PREFIX) -> dict[str, Any]:
    """Ask the bridge for one ``kind`` and wait for its answer (``REQUEST_SEC``).

    Raises ``RequestError`` when the bus is not connected, when nothing answers within
    ``timeout``, when the bridge answers with an error (the scaffold answers "not
    built" for every kind), or when the answer does not name this request and kind.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown request kind {kind!r}; one of {', '.join(KINDS)}")
    nc = bus._nc  # noqa: SLF001 - the bus keeps the connection; a request is its own subject
    if nc is None or not bus._connected:  # noqa: SLF001
        raise RequestError("the bus is not connected")
    request_id = _new_id()
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
        try:
            msg = await asyncio.wait_for(answers.get(), timeout)
        except asyncio.TimeoutError:
            raise RequestError(f"the bridge did not answer a {kind} request within {timeout:.0f} s") from None
        data = getattr(msg, "data", b"")
        return _decode_answer(data if isinstance(data, (bytes, bytearray)) else b"", request_id, kind)
    finally:
        try:
            await sub.unsubscribe()
        except Exception:  # noqa: BLE001 - the connection may be gone; the answer is what counts
            pass
