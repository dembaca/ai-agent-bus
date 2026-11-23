"""The smallest server that answers the bus's real contract.

Every HTTP answer here was measured, not invented: `scripts/probe_bus_calls.py`
runs the real `MattermostAdapter` against a recording stub and prints the 16
endpoints it calls. `KNOWN` is that table, so this stub answers what the bus
asked for rather than what a reader of the source guessed it would ask.

An unknown call is a 404 **and** a line in `.unknown`: a run asserts "0 unknown"
instead of passing because nothing checked. Every call lands in `.calls`.

The WebSocket half answers the `authentication_challenge` with `hello`, then
writes the frames the caller scripted. Mattermost sends a `posted` frame with
the post as JSON text and also as an object (the probes saw both); `posted_frame`
builds either, so a test never hand-rolls the JSON.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any

from .gateway import Gateway, WsConn

M = "m" * 26  # the ids the probe's answers use, so a caller compares like with like
TEAM = "t" * 26
CHANNEL = "c" * 26
POST = "p" * 26
USER = "u" * 26

# From the measured probe (probe_bus_calls.py): (method, path) regex -> body.
# Copied rather than imported because that file is a script with side effects,
# and because a change here must be a deliberate claim about the bus.
KNOWN: "OrderedDict[str, object]" = OrderedDict([
    (r"GET /api/v4/users/me$", {"id": M, "username": "agent-probe", "create_at": 1}),
    (r"GET /api/v4/teams/name/[^/]+$", {"id": TEAM}),
    (r"GET /api/v4/teams/[^/]+/channels/name/[^/]+$", {"id": CHANNEL}),
    (r"GET /api/v4/channels/[^/]+$", {"id": CHANNEL, "name": "agents", "type": "O"}),
    (r"GET /api/v4/channels/[^/]+/posts.*$", {"order": [], "posts": {}}),
    (r"GET /api/v4/channels/[^/]+/pinned$", {"order": [], "posts": {}}),
    (r"GET /api/v4/users/me/channels$", []),
    (r"GET /api/v4/users/me/teams/[^/]+/channels$", []),
    (r"POST /api/v4/posts$", {"id": POST, "user_id": M, "channel_id": CHANNEL,
                              "message": "hi", "create_at": 2}),
    (r"POST /api/v4/channels/members/me/view$", {}),
    (r"POST /api/v4/channels/[^/]+/members$", {}),
    (r"GET /api/v4/users/me/teams/[^/]+/channels/members$", []),
    (r"GET /api/v4/posts/[^/]+/thread$", {"order": [], "posts": {}}),
    (r"GET /api/v4/posts/[^/]+/reactions$", []),
    (r"GET /api/v4/channels/[^/]+/members/[^/]+$", {"channel_id": CHANNEL, "user_id": M}),
    (r"POST /api/v4/users/ids$", []),
    (r"POST /api/v4/users/usernames$", []),
    (r"POST /api/v4/users/status/ids$", []),
    (r"PUT /api/v4/users/me/status/custom$", {}),
    (r"POST /api/v4/reactions$", {}),
    (r"GET /api/v4/posts/[^/]+$", {"id": POST, "channel_id": CHANNEL, "message": "hi",
                                  "user_id": M, "create_at": 2}),
])


def posted_frame(post: dict[str, Any], *, as_object: bool = False,
                 channel_type: str = "O") -> dict[str, Any]:
    """A `posted` event; Mattermost sends `data.post` as text or as an object."""
    return {"event": "posted", "broadcast": {"channel_id": post.get("channel_id", CHANNEL)},
            "data": {"channel_type": channel_type,
                     "post": dict(post) if as_object else json.dumps(post)}}


def post_edited_frame(post: dict[str, Any], *, as_object: bool = False) -> dict[str, Any]:
    return {"event": "post_edited",
            "data": {"post": dict(post) if as_object else json.dumps(post)}}


def reaction_added_frame(reaction: dict[str, Any], channel_id: str = CHANNEL) -> dict[str, Any]:
    return {"event": "reaction_added", "broadcast": {"channel_id": channel_id},
            "data": {"reaction": json.dumps(reaction)}}


def user_added_frame(user_id: str = USER) -> dict[str, Any]:
    return {"event": "user_added", "data": {"user_id": user_id}}


def user_removed_frame(user_id: str = USER) -> dict[str, Any]:
    return {"event": "user_removed", "data": {"user_id": user_id}}


class MattermostStub:
    """HTTP answers from the measured table, and a scripted event socket."""

    def __init__(self, frames: list[dict[str, Any]] | None = None) -> None:
        self.frames: list[dict[str, Any]] = list(frames or [])
        self.calls: list[tuple[str, str]] = []
        self.unknown: list[tuple[str, str]] = []
        self.received: list[dict[str, Any]] = []
        self.authenticated = False
        self._patterns = [(re.compile(pattern), body) for pattern, body in KNOWN.items()]
        self._gateway = Gateway(self._on_http, self._on_ws)

    @property
    def url(self) -> str:
        return self._gateway.url

    async def __aenter__(self) -> MattermostStub:
        await self._gateway.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._gateway.stop()

    async def _on_http(self, method: str, target: str, _headers: dict[str, str],
                       _body: bytes) -> tuple[int, dict[str, str], bytes]:
        path = target.split("?", 1)[0]
        self.calls.append((method, path))
        key = f"{method} {path}"
        for pattern, body in self._patterns:
            if pattern.fullmatch(key):
                return 200, {"content-type": "application/json"}, json.dumps(body).encode()
        self.unknown.append((method, path))
        return 404, {"content-type": "application/json"}, b'{"message":"stub: unknown call"}'

    async def _on_ws(self, conn: WsConn, _headers: dict[str, str]) -> None:
        first = await conn.recv()
        if first is not None and first[0] == 0x1:
            try:
                message = json.loads(first[1])
            except json.JSONDecodeError:
                message = {}
            self.received.append(message)
            if message.get("action") == "authentication_challenge":
                self.authenticated = True
                await conn.send_text(json.dumps({"event": "hello"}))
        for frame in list(self.frames):
            await conn.send_text(frame if isinstance(frame, str) else json.dumps(frame))
        async for raw in conn:
            try:
                self.received.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
