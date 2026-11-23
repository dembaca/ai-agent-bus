"""The smallest server that answers the bus's real contract.

Every HTTP answer here was measured, not invented: `scripts/probe_bus_calls.py`
runs the real `MattermostAdapter` against a recording stub and prints the 16
endpoints it calls. `KNOWN` is that table, so this stub answers what the bus
asked for rather than what a reader of the source guessed it would ask.

The three rows beyond that probe were found by running the real server
(`bus.BusProcess`): the heartbeat's status write, the roster read, and the exit
path's unregister. They are marked in the table; a change there is still a
deliberate claim about the bus, which is why they are listed rather than guessed.

An unknown call is a 404 **and** a line in `.unknown`: a run asserts "0 unknown"
instead of passing because nothing checked. Every call lands in `.calls`.

The WebSocket half answers the `authentication_challenge` with `hello`, then
writes the frames the caller scripted. Mattermost sends a `posted` frame with
the post as JSON text and also as an object (the probes saw both); `posted_frame`
builds either, so a test never hand-rolls the JSON. `push` writes one now, into
whatever socket the bus opened: it connects on its first wait, not at start.

Two bots are told apart by the token each presents (`identify`), and a created
post is kept (`post_of`) so the frame a run pushes is the post the bus asked for.
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
    # Measured by driving the real server (`bus.bus_run`): the heartbeat writes
    # a status and the roster reads the channel's accounts. Both used to be
    # gaps; a 404 here is not a delivery fault, and reporting it as one buried
    # the numbers the run exists for.
    (r"PUT /api/v4/users/[^/]+/status$", {}),
    (r"GET /api/v4/users$", []),
    # The server's exit path unregisters the bot it was handed (client.
    # shutdown_sync): a bot does go offline when the server stops, and a run
    # that treated this as unknown would report four gaps per run.
    (r"DELETE /register/v1/agents/[^/]+$", {}),
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
        #: the upgraded sockets, oldest first: the bus opens its inbox on the
        #: first wait, not at start, so a run pushes into whatever is open
        self.conns: list[WsConn] = []
        #: token -> the bot it authenticates, when a caller named one
        self.bots: dict[str, dict[str, Any]] = {}
        #: post id -> the post the caller created, for `posted_frame`
        self.posts: dict[str, dict[str, Any]] = {}
        self._patterns = [(re.compile(pattern), body) for pattern, body in KNOWN.items()]
        self._gateway = Gateway(self._on_http, self._on_ws)

    @property
    def url(self) -> str:
        return self._gateway.url

    def identify(self, token: str, user_id: str, username: str) -> None:
        """Name the bot a token authenticates: two servers pointed at one stub
        are told apart by the bearer each sends, as Mattermost tells two bots
        apart. Unnamed tokens get the probe's bot (`M`, agent-probe)."""
        self.bots[token] = {"id": user_id, "username": username, "create_at": 1}

    def post_of(self, post_id: str) -> dict[str, Any]:
        """The post the caller created, as Mattermost's `posted` frame carries
        it. A run never invents a post: this is the one the bus asked for."""
        return dict(self.posts.get(post_id) or {"id": post_id, "channel_id": CHANNEL,
                                                "message": "", "create_at": 0})

    def _bot(self, headers: dict[str, str]) -> dict[str, Any]:
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        return self.bots.get(token) or {"id": M, "username": "agent-probe", "create_at": 1}

    async def push(self, frame: dict[str, Any] | str) -> int:
        """Send one frame now on every open socket, the count it reached.

        Zero means the bus has not opened its inbox yet — it does so on its
        first wait_for_events — and the frame reached nobody. That is visible
        here rather than a silent success for a run that wants it delivered.
        """
        text = frame if isinstance(frame, str) else json.dumps(frame)
        sent = 0
        for conn in list(self.conns):
            await conn.send_text(text)
            sent += 1
        return sent

    async def __aenter__(self) -> MattermostStub:
        await self._gateway.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._gateway.stop()

    async def _on_http(self, method: str, target: str, headers: dict[str, str],
                       body: bytes) -> tuple[int, dict[str, str], bytes]:
        path = target.split("?", 1)[0]
        self.calls.append((method, path))
        if method == "POST" and path == "/api/v4/posts":
            return self._create_post(headers, body)
        key = f"{method} {path}"
        for pattern, answer in self._patterns:
            if pattern.fullmatch(key):
                if pattern.pattern == r"GET /api/v4/users/me$":
                    answer = self._bot(headers)
                return 200, {"content-type": "application/json"}, json.dumps(answer).encode()
        self.unknown.append((method, path))
        return 404, {"content-type": "application/json"}, b'{"message":"stub: unknown call"}'

    def _create_post(self, headers: dict[str, str],
                     body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Mattermost's own answer to a create: the post, with the author the
        bearer names. Kept, so a run reads back exactly what it sent."""
        try:
            asked = json.loads(body or b"{}")
        except json.JSONDecodeError:
            asked = {}
        bot = self._bot(headers)
        seq = len(self.posts) + 1
        post = {"id": f"p{seq:025d}", "user_id": bot["id"], "channel_id": CHANNEL,
                "root_id": str(asked.get("root_id") or ""),
                "message": str(asked.get("message") or ""),
                "props": asked.get("props") or {},
                "create_at": 1_700_000_000_000 + seq}
        self.posts[post["id"]] = post
        return 200, {"content-type": "application/json"}, json.dumps(post).encode()

    async def _on_ws(self, conn: WsConn, _headers: dict[str, str]) -> None:
        self.conns.append(conn)
        try:
            await self._serve_ws(conn)
        finally:
            if conn in self.conns:
                self.conns.remove(conn)

    async def _serve_ws(self, conn: WsConn) -> None:
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
